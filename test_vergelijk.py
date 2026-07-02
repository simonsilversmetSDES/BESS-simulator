"""
Tests voor tariff_simpel, bereken_kosten, vergelijk_zonder_met (bess_core)
en de profiel-op-jaar-mapping (bess_backtest). Geen netwerktoegang nodig.
"""

import numpy as np
import pandas as pd
import pytest

from bess_backtest import _leg_profiel_op_jaar, _profiel_lookup
from bess_core import (
    BatteryParams,
    FinancialParams,
    bereken_kosten,
    summarize_lp,
    tariff_simpel,
    vergelijk_zonder_met,
)


def _bat(**kwargs):
    base = dict(capacity_kwh=20.0, crate="1 op 2", soc_start=0.5,
                dod=0.8, efficiency=0.95, da_charging=False)
    base.update(kwargs)
    return BatteryParams(**base)


def _week_df(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = 96 * 7
    ts = pd.date_range("2023-09-01", periods=n, freq="15min")
    pv = np.tile([0.0, 0.0, 8.0, 8.0], n // 4)
    cons = 4.0 + rng.random(n)
    da = 80.0 + 50.0 * np.sin(np.arange(n) / 96 * 2 * np.pi) + 10 * rng.standard_normal(n)
    return pd.DataFrame({"productie": pv, "consumptie": cons, "da_prijs": da}, index=ts)


# ===========================================================================
# tariff_simpel
# ===========================================================================

class TestTariffSimpel:
    def test_defaults(self):
        t = tariff_simpel()
        assert t.netkost_afname_eur_kwh == pytest.approx(0.003)
        assert t.toeslagen_afname_eur_kwh == 0.0
        assert t.capaciteitstarief_eur_kw_maand == pytest.approx(40.0 / 12.0)
        assert t.injectievergoeding_basis == "da"
        assert t.injectie_da_factor == 1.0

    def test_jaar_naar_maand_omrekening(self):
        t = tariff_simpel(cap_eur_kw_jaar=60.0)
        assert t.capaciteitstarief_eur_kw_maand == pytest.approx(5.0)

    def test_custom_var_netkost(self):
        t = tariff_simpel(var_netkost_eur_kwh=0.05)
        assert t.netkost_afname_eur_kwh == pytest.approx(0.05)


# ===========================================================================
# bereken_kosten
# ===========================================================================

class TestBerekenKosten:
    def test_componenten_sommeren_tot_totaal(self):
        n = 96
        ts = pd.date_range("2023-01-01", periods=n, freq="15min")
        g_imp = np.full(n, 2.0)
        g_exp = np.full(n, 0.5)
        da = np.full(n, 100.0)
        t = tariff_simpel()
        k = bereken_kosten(g_imp, g_exp, da, ts, t)
        verwacht = (k["energie_eur"] + k["netkost_var_eur"] + k["capaciteit_eur"]
                    - k["injectie_opbrengst_eur"])
        assert k["totaal_eur"] == pytest.approx(verwacht)

    def test_capaciteit_gebruikt_maandpiek(self):
        # Twee maanden, piek 8 kWh/kwartier = 32 kW in maand 1, 2 kWh = 8 kW in maand 2
        ts1 = pd.date_range("2023-01-01", periods=96, freq="15min")
        ts2 = pd.date_range("2023-02-01", periods=96, freq="15min")
        ts = ts1.append(ts2)
        g_imp = np.concatenate([np.full(96, 2.0), np.full(96, 2.0)])
        g_imp[10] = 8.0
        t = tariff_simpel(cap_eur_kw_jaar=12.0)   # 1 €/kW/maand
        k = bereken_kosten(g_imp, np.zeros(192), np.full(192, 50.0), ts, t)
        assert k["totaal_eur"] > 0
        assert k["capaciteit_eur"] == pytest.approx(32.0 * 1.0 + 8.0 * 1.0)
        assert k["piek_per_maand_kw"]["2023-01"] == pytest.approx(32.0)
        assert k["piek_per_maand_kw"]["2023-02"] == pytest.approx(8.0)

    def test_minimumpiek_toegepast(self):
        n = 96
        ts = pd.date_range("2023-01-01", periods=n, freq="15min")
        g_imp = np.full(n, 0.1)   # piek 0,4 kW < minimum 2,5 kW
        t = tariff_simpel(cap_eur_kw_jaar=12.0)
        k = bereken_kosten(g_imp, np.zeros(n), np.full(n, 50.0), ts, t)
        assert k["piek_per_maand_kw"]["2023-01"] == pytest.approx(2.5)

    def test_negatieve_da_geeft_negatieve_energiekost(self):
        n = 96
        ts = pd.date_range("2023-01-01", periods=n, freq="15min")
        g_imp = np.full(n, 2.0)
        k = bereken_kosten(g_imp, np.zeros(n), np.full(n, -50.0), ts, tariff_simpel())
        assert k["energie_eur"] < 0        # betaald worden om af te nemen
        assert k["netkost_var_eur"] > 0    # netkost blijft altijd positief (§8.6)


# ===========================================================================
# vergelijk_zonder_met
# ===========================================================================

class TestVergelijkZonderMet:
    def test_besparing_nooit_negatief(self):
        """Batterij stilhouden is altijd een geldige LP-oplossing, dus de
        geoptimaliseerde kost kan nooit hoger zijn dan zonder batterij."""
        uit = vergelijk_zonder_met(_week_df(), _bat(), tariff_simpel())
        assert uit["besparing_eur"] >= -1e-6

    def test_structuur_output(self):
        uit = vergelijk_zonder_met(_week_df(), _bat(), tariff_simpel())
        assert set(uit) == {"zonder", "met", "besparing_eur", "res"}
        assert uit["besparing_eur"] == pytest.approx(
            uit["zonder"]["totaal_eur"] - uit["met"]["totaal_eur"]
        )

    def test_consistent_met_summarize_lp(self):
        """besparing_eur (vergelijk) == jaarbaten + maintenance (summarize_lp):
        beide zijn de werkelijke kostendelta, summarize trekt er onderhoud af."""
        df = _week_df()
        bat = _bat()
        t = tariff_simpel()
        fin = FinancialParams()
        uit = vergelijk_zonder_met(df, bat, t)
        out = summarize_lp(uit["res"], bat, fin, t)
        maintenance = bat.capacity_kwh * fin.battery_price_eur_per_kwh * fin.maintenance_frac
        assert uit["besparing_eur"] == pytest.approx(
            out["financial"]["jaarbaten_eur"] + maintenance, abs=0.01
        )

    def test_timestamp_kolom_geaccepteerd(self):
        df = _week_df().reset_index(names="timestamp")
        uit = vergelijk_zonder_met(df, _bat(), tariff_simpel())
        assert uit["besparing_eur"] >= -1e-6

    def test_da_prijzen_vereist(self):
        df = _week_df().drop(columns="da_prijs")
        with pytest.raises(ValueError, match="DA-prijzen"):
            vergelijk_zonder_met(df, _bat(), tariff_simpel())


# ===========================================================================
# Profiel-op-jaar-mapping (backtest)
# ===========================================================================

class TestProfielMapping:
    def _profiel(self, jaar=2023):
        n = 96 * 365
        ts = pd.date_range(f"{jaar}-01-01", periods=n, freq="15min")
        return pd.DataFrame({
            "productie":  np.arange(n, dtype=float) % 10,
            "consumptie": np.full(n, 4.0),
        }, index=ts)

    def test_zelfde_jaar_identiek(self):
        profiel = self._profiel(2023)
        lookup = _profiel_lookup(profiel)
        prijzen = pd.Series(
            50.0,
            index=pd.date_range("2023-01-01", "2023-12-31 23:45", freq="15min",
                                tz="Europe/Brussels"),
            name="da_prijs",
        )
        df = _leg_profiel_op_jaar(lookup, prijzen)
        # DST maakt de tz-aware kalender 4 kwartieren korter (23:00→2:00 in maart
        # valt weg, oktober dubbel); waarden per kalenderdag+tijd moeten kloppen
        key = df.index.strftime("%m-%d %H:%M")[500]
        assert df["productie"].iloc[500] == lookup[key][0]

    def test_schrikkeldag_gebruikt_28_februari(self):
        profiel = self._profiel(2023)   # geen 29 februari in profiel
        lookup = _profiel_lookup(profiel)
        prijzen = pd.Series(
            50.0,
            index=pd.date_range("2024-02-28", "2024-03-01 23:45", freq="15min",
                                tz="Europe/Brussels"),
            name="da_prijs",
        )
        df = _leg_profiel_op_jaar(lookup, prijzen)
        feb28 = df[df.index.strftime("%m-%d") == "02-28"]["productie"].to_numpy()
        feb29 = df[df.index.strftime("%m-%d") == "02-29"]["productie"].to_numpy()
        assert np.allclose(feb28, feb29)

    def test_onvolledig_profiel_geeft_fout(self):
        profiel = self._profiel(2023).iloc[:96]   # slechts 1 dag
        lookup = _profiel_lookup(profiel)
        prijzen = pd.Series(
            50.0,
            index=pd.date_range("2023-06-01", periods=96, freq="15min",
                                tz="Europe/Brussels"),
            name="da_prijs",
        )
        with pytest.raises(ValueError, match="volledig jaarprofiel"):
            _leg_profiel_op_jaar(lookup, prijzen)
