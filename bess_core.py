"""
BESS Autoconsumptie & DA-arbitrage simulator — rekenkern.

Pure rekenlogica, geen UI/DB/HTTP. Input: kwartier-meterdata + batterijparameters.
Output: energiebalansen + financiele businesscase.

Vertaald uit Autoconsumptie_berekeningsfile.xlsx (tab 'In PU').
Rekent in absolute kWh per kwartier (niet per-unit zoals de Excel).
"""

from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
import pulp

# One-way: laadverliezen = 0, ontlaadverliezen = (1-η)·d_gross.
# Consistent met greedy simulate() en §8.7-validatieankers.
_EFF_CONVENTION = "one-way"


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

CRATE_DELERS = {"1 op 1": 1, "1 op 2": 2, "1 op 4": 4, "1 op 8": 8}


@dataclass
class BatteryParams:
    capacity_kwh: float = 600.0
    crate: str = "1 op 2"          # "1 op 1" | "1 op 2" | "1 op 4" | "1 op 8"
    soc_start: float = 0.5         # fractie van capaciteit
    dod: float = 0.8               # ontlaaddiepte (bruikbare fractie)
    efficiency: float = 0.95       # afgifte-rendement (one-way op ontladen)
    da_charging: bool = True       # slim laden op DA aan/uit
    da_price_window_steps: int = 96   # venster voor 'laagste prijs' (96 = 1 dag)
    pv_lookahead_steps: int = 96      # vooruitblik PV-productie (96 = 1 dag)
    pv_lookahead_threshold_kwh: float = None  # drempel verwachte PV; None = auto

    @property
    def power_per_quarter_kwh(self) -> float:
        """Max laad/ontlaadenergie per kwartier (kWh).
        Excel AD12: Capaciteit*DOD/crate_deler, toegepast als vermogen*0.25h.
        """
        deler = CRATE_DELERS[self.crate]
        power_kw = self.capacity_kwh * self.dod / deler
        return power_kw * 0.25

    @property
    def floor_kwh(self) -> float:
        """Onderste bruikbare grens (kWh)."""
        return self.capacity_kwh * (1 - self.dod)

    @property
    def start_level_kwh(self) -> float:
        return self.capacity_kwh * self.soc_start


@dataclass
class FinancialParams:
    battery_price_eur_per_kwh: float = 685.0
    maintenance_frac: float = 0.015      # per jaar, van batterijkost
    install_frac: float = 0.15           # eenmalig, van batterijkost
    lifetime_years: int = 16
    discount_rate: float = 0.06          # voor NPV
    td_surcharge: float = 1.3            # T&D-opslag op netafname (kostkant DA-laden)
    feed_in_tariff_eur_per_mwh: float = None  # vergoeding injectie; None = geen


@dataclass
class TariffParams:
    """Belgische tariefstructuur voor LP-kostenoptimalisatie (§8.1)."""
    netkost_afname_eur_kwh: float
    toeslagen_afname_eur_kwh: float
    netkost_injectie_eur_kwh: float
    injectievergoeding_basis: str               # 'da' | 'vast'
    injectievergoeding_vast_eur_kwh: float | None
    injectie_da_factor: float = 1.0
    capaciteitstarief_eur_kw_maand: float = 0.0
    cap_min_piek_kw: float = 2.5
    databeheer_eur_jaar: float = 0.0

    def injectie_vergoeding_per_kwh(self, da_eur_kwh: float) -> float:
        """
        Netto vergoeding per kWh geïnjecteerde energie.
        netkost_injectie_eur_kwh wordt ALLEEN hier afgetrokken — nooit elders.
        """
        if self.injectievergoeding_basis == "da":
            return da_eur_kwh * self.injectie_da_factor - self.netkost_injectie_eur_kwh
        return self.injectievergoeding_vast_eur_kwh - self.netkost_injectie_eur_kwh


# ---------------------------------------------------------------------------
# Datavoorbereiding: reconstructie productie/consumptie uit netto meterdata
# ---------------------------------------------------------------------------

def reconstruct_profiles(df: pd.DataFrame) -> pd.DataFrame:
    """
    Verwacht kolommen: afname, injectie (kWh/kwartier), optioneel pv_productie.
    Voegt 'productie' en 'consumptie' toe.

    Met PV-meting:  consumptie = afname + productie - injectie
    Zonder:         productie = injectie ; consumptie = afname
    """
    out = df.copy()
    if "pv_productie" in out.columns and out["pv_productie"].notna().any():
        out["productie"] = out["pv_productie"].fillna(0.0)
        out["consumptie"] = out["afname"] + out["productie"] - out["injectie"]
    else:
        out["productie"] = out["injectie"]
        out["consumptie"] = out["afname"]
    # clamp kleine negatieve ruis
    out["productie"] = out["productie"].clip(lower=0)
    out["consumptie"] = out["consumptie"].clip(lower=0)
    return out


# ---------------------------------------------------------------------------
# De simulatiemotor — kwartier voor kwartier (sequentieel, state-dragend)
# ---------------------------------------------------------------------------

def simulate(
    df: pd.DataFrame,
    battery: BatteryParams,
    da_prices_eur_mwh: np.ndarray | None = None,
) -> pd.DataFrame:
    """
    df: met kolommen 'productie','consumptie' (kWh/kwartier), 'da_prijs' optioneel.
    da_prices_eur_mwh: array gelijk aan len(df), of None (dan kolom 'da_prijs').
    Retourneert df met alle dispatch-kolommen toegevoegd.
    """
    n = len(df)
    prod = df["productie"].to_numpy(dtype=float)
    cons = df["consumptie"].to_numpy(dtype=float)

    if da_prices_eur_mwh is not None:
        da = np.asarray(da_prices_eur_mwh, dtype=float)
    elif "da_prijs" in df.columns:
        da = df["da_prijs"].to_numpy(dtype=float)
    else:
        da = np.zeros(n)

    cap = battery.capacity_kwh
    floor = battery.floor_kwh
    eff = battery.efficiency
    plim = battery.power_per_quarter_kwh

    # PV-vooruitblik drempel: default = gemiddelde dag-PV * factor
    if battery.pv_lookahead_threshold_kwh is None:
        daily_pv = prod.reshape(-1)[: (n // 96) * 96].reshape(-1, 96).sum(axis=1)
        thr = (daily_pv.mean() * 0.7) if len(daily_pv) else 0.0
    else:
        thr = battery.pv_lookahead_threshold_kwh

    # output arrays
    H = prod - cons
    T = np.zeros(n)   # charging PV
    W = np.zeros(n)   # charging DA (net)
    R = np.zeros(n)   # discharging (negatief)
    level = np.zeros(n)  # niveau na verliezen (Z), start volgend kwartier

    prev = battery.start_level_kwh

    for t in range(n):
        h = H[t]
        headroom = cap - prev

        # --- PV-laden (T) ---
        if h >= 0:
            t_charge = min(h, plim, headroom)
            t_charge = max(t_charge, 0.0)
        else:
            t_charge = 0.0

        # --- Slim DA-laden (W) ---
        w_charge = 0.0
        if battery.da_charging and t_charge == 0.0:
            win_end = min(t + battery.da_price_window_steps, n)
            is_min_price = da[t] <= da[t:win_end].min() if win_end > t else False
            la_end = min(t + battery.pv_lookahead_steps, n)
            pv_ahead = prod[t:la_end].sum()
            if is_min_price and pv_ahead < thr:
                w_charge = min(plim, headroom - t_charge)
                w_charge = max(w_charge, 0.0)

        s_charge = t_charge + w_charge

        # --- Ontladen (R) ---
        # (prev - floor) / (2 - eff): corrigeert voor afgifteverliezen die na de
        # niveau-update worden afgetrokken, zodat level nooit onder floor_kwh zakt.
        available = (prev - floor) / (2 - eff)
        if h < 0 and w_charge == 0.0:
            need = abs(h)
            r_dis = -min(need, plim, max(available, 0.0))
        else:
            r_dis = 0.0

        # --- Niveau-update met afgifteverlies ---
        raw = prev + r_dis + s_charge
        losses = abs(r_dis) - abs(eff * r_dis)   # verlies op ontladen
        lvl = raw - losses

        T[t] = t_charge
        W[t] = w_charge
        R[t] = r_dis
        level[t] = lvl
        prev = lvl

    res = df.copy()
    res["H"] = H
    res["charge_pv"] = T
    res["charge_da"] = W
    res["discharge"] = R
    res["level"] = level
    res["da_prijs"] = da

    # energiebalansen
    res["ac_zonder"] = np.where(H < 0, prod, cons)
    res["ac_met"] = res["ac_zonder"] + T * eff
    res["inj_zonder"] = np.clip(H, 0, None)
    res["inj_met"] = res["inj_zonder"] - (T * eff + W * eff)
    res["afn_zonder"] = cons - prod + res["inj_zonder"]
    res["afn_met"] = cons - prod + res["inj_met"]
    return res


# ---------------------------------------------------------------------------
# Aggregatie + businesscase
# ---------------------------------------------------------------------------

def summarize(res: pd.DataFrame, battery: BatteryParams, fin: FinancialParams,
              energy_price_eur_mwh: float = 180.0) -> dict:
    """Aggregeert simulatie en bouwt de businesscase."""
    eff = battery.efficiency
    to_mwh = 1 / 1000

    energy = {
        "ac_zonder_mwh": res["ac_zonder"].sum() * to_mwh,
        "ac_met_mwh": res["ac_met"].sum() * to_mwh,
        "inj_zonder_mwh": res["inj_zonder"].sum() * to_mwh,
        "inj_met_mwh": res["inj_met"].sum() * to_mwh,
        "afn_zonder_mwh": res["afn_zonder"].sum() * to_mwh,
        "afn_met_mwh": res["afn_met"].sum() * to_mwh,
        "charge_pv_mwh": res["charge_pv"].sum() * to_mwh,
        "charge_da_mwh": res["charge_da"].sum() * to_mwh,
        "discharge_mwh": abs(res["discharge"].sum()) * to_mwh,
    }
    energy["extra_ac_mwh"] = energy["ac_met_mwh"] - energy["ac_zonder_mwh"]

    # --- Financieel ---
    battery_cost = battery.capacity_kwh * fin.battery_price_eur_per_kwh
    investment = battery_cost * (1 + fin.install_frac)
    maintenance_yr = battery_cost * fin.maintenance_frac

    # Besparing autoconsumptie: minder netafname * energieprijs
    afname_reductie_mwh = energy["afn_zonder_mwh"] - energy["afn_met_mwh"]
    besparing_ac_eur = afname_reductie_mwh * energy_price_eur_mwh

    # DA-arbitrage: ontladen energie verkocht tegen DA, geladen uit net gekocht
    # tegen DA*T&D. (Tekenconventie GECORRIGEERD t.o.v. Excel-bug.)
    da = res["da_prijs"].to_numpy()
    discharge_kwh = -res["discharge"].to_numpy()  # positief
    charge_da_kwh = res["charge_da"].to_numpy()
    verkoop_eur = (discharge_kwh * eff * da * to_mwh).sum()
    kost_da_eur = (charge_da_kwh * da * fin.td_surcharge * to_mwh).sum()
    da_arbitrage_eur = verkoop_eur - kost_da_eur

    jaarbaten = besparing_ac_eur + da_arbitrage_eur - maintenance_yr

    # NPV / IRR / breakeven
    cashflows = [-investment] + [jaarbaten] * fin.lifetime_years
    npv = npv_calc(fin.discount_rate, cashflows)
    irr = irr_calc(cashflows)
    cum = np.cumsum(cashflows)
    breakeven = next((i for i, v in enumerate(cum) if v >= 0), None)
    roi = (sum(cashflows[1:]) - investment) / investment if investment else None

    return {
        "energy": energy,
        "financial": {
            "investment_eur": investment,
            "maintenance_yr_eur": maintenance_yr,
            "besparing_autoconsumptie_eur": besparing_ac_eur,
            "da_arbitrage_eur": da_arbitrage_eur,
            "da_verkoop_eur": verkoop_eur,
            "da_kost_eur": kost_da_eur,
            "jaarbaten_eur": jaarbaten,
            "npv_eur": npv,
            "irr": irr,
            "breakeven_jaar": breakeven,
            "roi": roi,
        },
    }


def npv_calc(rate: float, cashflows: list[float]) -> float:
    return sum(cf / (1 + rate) ** i for i, cf in enumerate(cashflows))


def irr_calc(cashflows: list[float], guess: float = 0.1) -> float | None:
    """Newton-achtige IRR; None als niet convergeert (i.p.v. #NUM!)."""
    cf = np.array(cashflows, dtype=float)
    if not (np.any(cf > 0) and np.any(cf < 0)):
        return None  # geen tekenwissel -> IRR bestaat niet
    rate = guess
    for _ in range(200):
        denom = (1 + rate) ** np.arange(len(cf))
        npv = (cf / denom).sum()
        d_npv = (-np.arange(len(cf)) * cf / denom / (1 + rate)).sum()
        if abs(d_npv) < 1e-12:
            break
        new = rate - npv / d_npv
        if abs(new - rate) < 1e-7:
            return new
        rate = new
    return rate if -0.999 < rate < 10 else None


# Alias voor regressievergelijking
simulate_greedy = simulate


# ---------------------------------------------------------------------------
# LP-dispatch helpers
# ---------------------------------------------------------------------------

_LP_TIMELIMIT = 120  # CBC timeout in seconden per kalendermaand


def _solve_month_lp(
    prod_m: np.ndarray,
    cons_m: np.ndarray,
    da_m: np.ndarray,
    battery: BatteryParams,
    tariff: TariffParams,
    soc_start: float,
    maand_label: str,
) -> tuple[dict[str, np.ndarray], float]:
    """
    Löst één LP voor een kalendermaand. Retourneert (resultaat_dict, soc_einde).

    Efficiëntie-conventie: one-way (zie _EFF_CONVENTION).
    - Laden: volledig naar SOC (geen laadverlies).
    - Ontladen: d_gross daalt van SOC; netto geleverd = d_gross * η.
    - SOC: soc[t] = soc[t-1] + c_pv[t] + c_grid[t] - d_self[t] - d_inj[t]
    - Energiebalans: g_imp - g_exp = cons - prod + c_pv + c_grid - (d_self+d_inj)*η
    """
    n = len(prod_m)
    cap = battery.capacity_kwh
    floor = battery.floor_kwh
    eff = battery.efficiency
    plim = battery.power_per_quarter_kwh
    netkost = tariff.netkost_afname_eur_kwh
    toes = tariff.toeslagen_afname_eur_kwh
    cap_tar = tariff.capaciteitstarief_eur_kw_maand

    prob = pulp.LpProblem(f"bess_{maand_label}", pulp.LpMinimize)

    c_pv   = [pulp.LpVariable(f"c_pv_{t}",   lowBound=0) for t in range(n)]
    c_grid = [pulp.LpVariable(f"c_grid_{t}", lowBound=0) for t in range(n)]
    d_self = [pulp.LpVariable(f"d_self_{t}", lowBound=0) for t in range(n)]
    d_inj  = [pulp.LpVariable(f"d_inj_{t}",  lowBound=0) for t in range(n)]
    soc    = [pulp.LpVariable(f"soc_{t}",    lowBound=floor, upBound=cap) for t in range(n)]
    g_imp  = [pulp.LpVariable(f"g_imp_{t}",  lowBound=0) for t in range(n)]
    g_exp  = [pulp.LpVariable(f"g_exp_{t}",  lowBound=0) for t in range(n)]
    peak   = pulp.LpVariable("peak", lowBound=tariff.cap_min_piek_kw)

    # Doelfunctie (€/kWh; DA in €/MWh → /1000)
    prob += (
        pulp.lpSum(
            g_imp[t] * (da_m[t] / 1000.0 + netkost + toes)
            - g_exp[t] * tariff.injectie_vergoeding_per_kwh(da_m[t] / 1000.0)
            for t in range(n)
        )
        + peak * cap_tar
    )

    for t in range(n):
        pv_overschot = max(prod_m[t] - cons_m[t], 0.0)
        rest_vraag   = max(cons_m[t] - prod_m[t], 0.0)

        # SOC-continuïteit
        soc_prev = soc_start if t == 0 else soc[t - 1]
        prob += soc[t] == soc_prev + c_pv[t] + c_grid[t] - d_self[t] - d_inj[t]

        # Vermogenslimieten
        prob += c_pv[t] + c_grid[t] <= plim
        prob += d_self[t] + d_inj[t] <= plim

        # PV-laden ≤ PV-overschot
        prob += c_pv[t] <= pv_overschot

        # Eigenverbruiksontlading ≤ resterende vraag (netto geleverd = d_self·η)
        prob += d_self[t] * eff <= rest_vraag

        # Energiebalans aansluitpunt
        # g_imp - g_exp = cons - prod + c_pv + c_grid - (d_self + d_inj)*η
        prob += (
            g_imp[t] - g_exp[t]
            == cons_m[t] - prod_m[t] + c_pv[t] + c_grid[t]
            - (d_self[t] + d_inj[t]) * eff
        )

        # Maandpiek: peak ≥ g_imp[t] / 0.25h (kW)
        prob += peak >= g_imp[t] / 0.25

    prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=_LP_TIMELIMIT))

    if pulp.LpStatus[prob.status] != "Optimal":
        raise RuntimeError(
            f"LP maand {maand_label}: status={pulp.LpStatus[prob.status]}. "
            "Controleer inputdata of vergroot timeLimit."
        )

    def v(var_list):
        return np.array([pulp.value(x) or 0.0 for x in var_list])

    res = {
        "c_pv":   v(c_pv),
        "c_grid": v(c_grid),
        "d_self": v(d_self),
        "d_inj":  v(d_inj),
        "soc":    v(soc),
        "g_imp":  v(g_imp),
        "g_exp":  v(g_exp),
    }
    soc_end = float(pulp.value(soc[-1]) or 0.0)
    return res, soc_end


def simulate_lp(
    df: pd.DataFrame,
    battery: BatteryParams,
    tariff: TariffParams,
    da_prices_eur_mwh: np.ndarray | None = None,
) -> pd.DataFrame:
    """
    LP-kostenoptimaliserende BESS-dispatch (Variant A: één LP per kalendermaand).

    Efficiëntie-conventie: one-way (_EFF_CONVENTION). Laden zonder verlies;
    ontladen levert d·η aan het net/verbruik terwijl de SOC daalt met d (gross).

    Bekende beperking: elke maand wordt onafhankelijk opgelost met vaste SOC-start
    (carry-over van vorige maand). Maand M optimaliseert niet naar een doelSOC,
    waardoor het resultaat suboptimaal kan zijn op maandgrenzen (±1 dag effect).

    df: DataFrame met kolommen 'productie', 'consumptie' (kWh/kwartier),
        optioneel 'da_prijs' (€/MWh) en een DatetimeIndex of 'timestamp'-kolom.
    """
    n = len(df)
    prod = df["productie"].to_numpy(dtype=float)
    cons = df["consumptie"].to_numpy(dtype=float)

    if da_prices_eur_mwh is not None:
        da = np.asarray(da_prices_eur_mwh, dtype=float)
    elif "da_prijs" in df.columns:
        da = df["da_prijs"].to_numpy(dtype=float)
    else:
        da = np.zeros(n)

    # Bepaal maandindex op basis van timestamp
    if isinstance(df.index, pd.DatetimeIndex):
        ts = df.index
    elif "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"])
    else:
        ts = pd.date_range("2020-01-01", periods=n, freq="15min")

    month_keys = ts.to_period("M")
    unique_months = month_keys.unique()

    out_arrays: dict[str, list] = {k: [] for k in
        ["c_pv", "c_grid", "d_self", "d_inj", "soc", "g_imp", "g_exp"]}

    soc_carry = battery.start_level_kwh

    for mperiod in unique_months:
        mask = month_keys == mperiod
        idx = np.where(mask)[0]
        label = str(mperiod)

        month_res, soc_carry = _solve_month_lp(
            prod[idx], cons[idx], da[idx],
            battery, tariff, soc_carry, label,
        )
        for k in out_arrays:
            out_arrays[k].append(month_res[k])

    # Concateneer maanden
    c_pv   = np.concatenate(out_arrays["c_pv"])
    c_grid = np.concatenate(out_arrays["c_grid"])
    d_self = np.concatenate(out_arrays["d_self"])
    d_inj  = np.concatenate(out_arrays["d_inj"])
    soc    = np.concatenate(out_arrays["soc"])
    g_imp  = np.concatenate(out_arrays["g_imp"])
    g_exp  = np.concatenate(out_arrays["g_exp"])

    eff = battery.efficiency
    H = prod - cons

    res = df.copy()
    res["H"]             = H
    res["charge_pv"]     = c_pv
    res["charge_da"]     = c_grid
    res["discharge"]     = -(d_self + d_inj)        # negatief teken, conform greedy
    res["level"]         = soc
    res["discharge_self"] = d_self
    res["discharge_inj"]  = d_inj
    res["g_imp"]         = g_imp
    res["g_exp"]         = g_exp
    res["da_prijs"]      = da

    # Terugwaartse-compatibele kolommen (zelfde definitie als greedy)
    res["ac_zonder"] = np.where(H < 0, prod, cons)
    res["ac_met"]    = res["ac_zonder"] + c_pv * eff
    res["inj_zonder"] = np.clip(H, 0, None)
    res["inj_met"]    = g_exp
    res["afn_zonder"] = cons - prod + res["inj_zonder"]
    res["afn_met"]    = g_imp
    return res


# ---------------------------------------------------------------------------
# LP-samenvatting met kostensplitsing (§8.5)
# ---------------------------------------------------------------------------

def summarize_lp(
    res: pd.DataFrame,
    battery: BatteryParams,
    fin: FinancialParams,
    tariff: TariffParams,
) -> dict:
    """
    Aggregeert LP-simulatie en berekent de kostensplitsing (§8.5).

    netkost_injectie_eur_kwh wordt ALLEEN verrekend via
    tariff.injectie_vergoeding_per_kwh() — nergens anders afgetrokken.
    besparing_energie_eur kan negatief zijn als da < 0 (minder importeren
    terwijl je betaald wordt = gemiste opbrengst).
    """
    eff = battery.efficiency
    to_mwh = 1 / 1000

    prod = res["productie"].to_numpy(dtype=float)
    cons = res["consumptie"].to_numpy(dtype=float)
    da   = res["da_prijs"].to_numpy(dtype=float)
    g_imp_met = res["g_imp"].to_numpy(dtype=float)
    g_exp_met = res["g_exp"].to_numpy(dtype=float)
    c_pv   = res["charge_pv"].to_numpy(dtype=float)
    c_grid = res["charge_da"].to_numpy(dtype=float)
    d_inj  = res["discharge_inj"].to_numpy(dtype=float)

    g_imp_zonder = np.maximum(cons - prod, 0.0)
    g_exp_zonder = np.maximum(prod - cons, 0.0)

    # Maandpiek zonder en met batterij
    if isinstance(res.index, pd.DatetimeIndex):
        ts = res.index
    elif "timestamp" in res.columns:
        ts = pd.to_datetime(res["timestamp"])
    else:
        ts = pd.date_range("2020-01-01", periods=len(res), freq="15min")

    month_keys = ts.to_period("M")

    peak_met_list = []
    peak_zonder_list = []
    for mperiod in month_keys.unique():
        mask = np.asarray(month_keys == mperiod)
        peak_met_list.append(g_imp_met[mask].max() / 0.25)
        peak_zonder_list.append(g_imp_zonder[mask].max() / 0.25)

    peak_met_total    = sum(max(p, tariff.cap_min_piek_kw) for p in peak_met_list)
    peak_zonder_total = sum(max(p, tariff.cap_min_piek_kw) for p in peak_zonder_list)

    da_kwh = da / 1000.0
    inj_verg = np.array([tariff.injectie_vergoeding_per_kwh(d) for d in da_kwh])

    besparing_energie_eur        = float((g_imp_zonder * da_kwh).sum() - (g_imp_met * da_kwh).sum())
    besparing_netkost_afname_eur = float(((g_imp_zonder - g_imp_met) * tariff.netkost_afname_eur_kwh).sum())
    besparing_toeslagen_eur      = float(((g_imp_zonder - g_imp_met) * tariff.toeslagen_afname_eur_kwh).sum())
    besparing_capaciteit_eur     = float(
        (peak_zonder_total - peak_met_total) * tariff.capaciteitstarief_eur_kw_maand
    )
    injectie_opbrengst_eur = float((g_exp_met * inj_verg).sum())
    arbitrage_marge_eur    = float(
        (d_inj * eff * inj_verg).sum()
        - (c_grid * (da_kwh + tariff.netkost_afname_eur_kwh + tariff.toeslagen_afname_eur_kwh)).sum()
    )

    energy = {
        "ac_zonder_mwh":  res["ac_zonder"].sum() * to_mwh,
        "ac_met_mwh":     res["ac_met"].sum() * to_mwh,
        "inj_zonder_mwh": res["inj_zonder"].sum() * to_mwh,
        "inj_met_mwh":    res["inj_met"].sum() * to_mwh,
        "afn_zonder_mwh": res["afn_zonder"].sum() * to_mwh,
        "afn_met_mwh":    res["afn_met"].sum() * to_mwh,
        "charge_pv_mwh":  c_pv.sum() * to_mwh,
        "charge_da_mwh":  c_grid.sum() * to_mwh,
        "discharge_mwh":  abs(res["discharge"].sum()) * to_mwh,
    }
    energy["extra_ac_mwh"] = energy["ac_met_mwh"] - energy["ac_zonder_mwh"]

    battery_cost   = battery.capacity_kwh * fin.battery_price_eur_per_kwh
    investment     = battery_cost * (1 + fin.install_frac)
    maintenance_yr = battery_cost * fin.maintenance_frac

    jaarbaten = (
        besparing_energie_eur
        + besparing_netkost_afname_eur
        + besparing_toeslagen_eur
        + besparing_capaciteit_eur
        + injectie_opbrengst_eur
        - maintenance_yr
    )

    cashflows = [-investment] + [jaarbaten] * fin.lifetime_years
    npv = npv_calc(fin.discount_rate, cashflows)
    irr = irr_calc(cashflows)
    cum = np.cumsum(cashflows)
    breakeven = next((i for i, v in enumerate(cum) if v >= 0), None)
    roi = (sum(cashflows[1:]) - investment) / investment if investment else None

    return {
        "energy": energy,
        "financial": {
            "investment_eur":               investment,
            "maintenance_yr_eur":           maintenance_yr,
            "besparing_energie_eur":        besparing_energie_eur,
            "besparing_netkost_afname_eur": besparing_netkost_afname_eur,
            "besparing_toeslagen_eur":      besparing_toeslagen_eur,
            "besparing_capaciteit_eur":     besparing_capaciteit_eur,
            "injectie_opbrengst_eur":       injectie_opbrengst_eur,
            "arbitrage_marge_eur":          arbitrage_marge_eur,
            "jaarbaten_eur":                jaarbaten,
            "npv_eur":                      npv,
            "irr":                          irr,
            "breakeven_jaar":               breakeven,
            "roi":                          roi,
        },
        "beperkingen": [
            "Maandgrens-suboptimaliteit: elke maand onafhankelijk opgelost.",
            f"Solver timeout per maand: {_LP_TIMELIMIT} s (CBC).",
        ],
    }
