"""
Grafiekdata voor het dashboard, berekend uit een LP-simulatieresultaat:

    stromen_per_kwartier(res, battery) -> DataFrame   # energiestromen per kwartier
    sankey_data(stromen)               -> dict        # jaarstromen in MWh (Sankey)
    uurprofiel_data(stromen)           -> list[dict]  # gemiddeld kW per uur v/d dag
    maandkosten_data(res, tariff)      -> list[dict]  # kost zonder/met per maand

Herkomstsplitsing batterij (PV- vs netgeladen energie): de batterij-inhoud wordt
proportioneel gemengd bijgehouden. De startinhoud wordt aan PV toegerekend —
op jaarschaal is die aanname verwaarloosbaar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bess_core import BatteryParams, TariffParams, _maand_periods, bereken_kosten


def stromen_per_kwartier(res: pd.DataFrame, battery: BatteryParams) -> pd.DataFrame:
    """
    Splitst het LP-resultaat in fysieke energiestromen (kWh per kwartier).

    Kolommen: pv_naar_verbruik, pv_naar_bess, pv_naar_net,
              net_naar_verbruik, net_naar_bess,
              bess_naar_verbruik (geleverd, na verlies),
              bess_naar_verbruik_uit_pv, bess_naar_verbruik_uit_net,
              bess_naar_net (geleverd), bess_verlies,
              consumptie, da_prijs
    """
    eff = battery.efficiency
    prod = res["productie"].to_numpy(dtype=float)
    cons = res["consumptie"].to_numpy(dtype=float)
    c_pv = res["charge_pv"].to_numpy(dtype=float)
    c_grid = res["charge_da"].to_numpy(dtype=float)
    d_self = res["discharge_self"].to_numpy(dtype=float)
    d_inj = res["discharge_inj"].to_numpy(dtype=float)
    g_imp = res["g_imp"].to_numpy(dtype=float)
    g_exp = res["g_exp"].to_numpy(dtype=float)

    pv_naar_verbruik = np.minimum(prod, cons)

    # Netladen kan alleen uit werkelijke import komen; laadt de batterij in een
    # PV-overschotkwartier via c_grid, dan is dat fysiek PV-energie.
    net_naar_bess = np.minimum(c_grid, g_imp)
    net_naar_verbruik = g_imp - net_naar_bess
    pv_naar_bess = c_pv + (c_grid - net_naar_bess)
    pv_naar_net = np.clip(g_exp - d_inj * eff, 0.0, None)

    # Herkomstsplitsing van de ontlading (proportioneel gemengde inhoud)
    n = len(prod)
    d_self_pv = np.zeros(n)
    d_self_net = np.zeros(n)
    inhoud_pv = battery.start_level_kwh   # startinhoud toegerekend aan PV
    inhoud_net = 0.0
    for t in range(n):
        inhoud_pv += pv_naar_bess[t]
        inhoud_net += net_naar_bess[t]
        totaal = inhoud_pv + inhoud_net
        ontlading = d_self[t] + d_inj[t]
        if ontlading > 0 and totaal > 0:
            f_pv = min(max(inhoud_pv / totaal, 0.0), 1.0)
            d_self_pv[t] = d_self[t] * f_pv
            d_self_net[t] = d_self[t] * (1.0 - f_pv)
            inhoud_pv -= ontlading * f_pv
            inhoud_net -= ontlading * (1.0 - f_pv)

    return pd.DataFrame(
        {
            "pv_naar_verbruik": pv_naar_verbruik,
            "pv_naar_bess": pv_naar_bess,
            "pv_naar_net": pv_naar_net,
            "net_naar_verbruik": net_naar_verbruik,
            "net_naar_bess": net_naar_bess,
            "bess_naar_verbruik": d_self * eff,
            "bess_naar_verbruik_uit_pv": d_self_pv * eff,
            "bess_naar_verbruik_uit_net": d_self_net * eff,
            "bess_naar_net": d_inj * eff,
            "bess_verlies": (d_self + d_inj) * (1.0 - eff),
            "consumptie": cons,
            "da_prijs": res["da_prijs"].to_numpy(dtype=float),
        },
        index=res.index,
    )


def sankey_data(stromen: pd.DataFrame) -> dict:
    """Jaartotalen als Sankey-links in MWh (bronnen links, doelen rechts)."""
    to_mwh = 1 / 1000
    s = stromen.sum()

    def link(van, naar, kwh):
        return {"van": van, "naar": naar, "mwh": round(float(kwh) * to_mwh, 3)}

    links = [
        link("PV", "Verbruik", s["pv_naar_verbruik"]),
        link("PV", "Batterij", s["pv_naar_bess"]),
        link("PV", "Net (injectie)", s["pv_naar_net"]),
        link("Net (import)", "Verbruik", s["net_naar_verbruik"]),
        link("Net (import)", "Batterij", s["net_naar_bess"]),
        link("Batterij", "Verbruik", s["bess_naar_verbruik"]),
        link("Batterij", "Net (injectie)", s["bess_naar_net"]),
        link("Batterij", "Verliezen", s["bess_verlies"]),
    ]
    return {"links": [l for l in links if l["mwh"] > 0]}


def uurprofiel_data(stromen: pd.DataFrame) -> list[dict]:
    """
    Gemiddeld vermogen (kW) per uur van de dag, uitgesplitst per stroom,
    plus de gemiddelde DA-prijs — voor het zelfverbruiksprofiel-diagram.
    """
    per_uur = stromen.groupby(stromen.index.hour).mean()

    rijen = []
    for uur, rij in per_uur.iterrows():
        rijen.append({
            "uur": int(uur),
            "pv_naar_verbruik_kw": round(float(rij["pv_naar_verbruik"]) * 4, 2),
            "bess_uit_pv_kw": round(float(rij["bess_naar_verbruik_uit_pv"]) * 4, 2),
            "bess_uit_net_kw": round(float(rij["bess_naar_verbruik_uit_net"]) * 4, 2),
            "net_naar_verbruik_kw": round(float(rij["net_naar_verbruik"]) * 4, 2),
            "consumptie_kw": round(float(rij["consumptie"]) * 4, 2),
            "da_gemiddeld_eur_mwh": round(float(rij["da_prijs"]), 2),
        })
    return rijen


def maandkosten_data(res: pd.DataFrame, tariff: TariffParams) -> list[dict]:
    """Kost zonder/met batterij per kalendermaand — voor de besparingsgrafiek."""
    prod = res["productie"].to_numpy(dtype=float)
    cons = res["consumptie"].to_numpy(dtype=float)
    da = res["da_prijs"].to_numpy(dtype=float)
    g_imp_zonder = np.maximum(cons - prod, 0.0)
    g_exp_zonder = np.maximum(prod - cons, 0.0)
    g_imp_met = res["g_imp"].to_numpy(dtype=float)
    g_exp_met = res["g_exp"].to_numpy(dtype=float)

    maanden = _maand_periods(res.index)
    rijen = []
    for mp in maanden.unique():
        m = np.asarray(maanden == mp)
        ts_m = res.index[m]
        zonder = bereken_kosten(g_imp_zonder[m], g_exp_zonder[m], da[m], ts_m, tariff)
        met = bereken_kosten(g_imp_met[m], g_exp_met[m], da[m], ts_m, tariff)
        rijen.append({
            "maand": str(mp),
            "kost_zonder_eur": round(float(zonder["totaal_eur"]), 2),
            "kost_met_eur": round(float(met["totaal_eur"]), 2),
            "besparing_eur": round(float(zonder["totaal_eur"] - met["totaal_eur"]), 2),
        })
    return rijen
