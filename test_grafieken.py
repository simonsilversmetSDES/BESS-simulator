"""
Tests voor bess_grafieken: energiebehoud in de stromensplitsing, en de
structuur van Sankey-, uurprofiel- en maandkostendata.
"""

import numpy as np
import pandas as pd
import pytest

from bess_core import BatteryParams, tariff_simpel, simulate_lp, vergelijk_zonder_met
from bess_grafieken import (
    maandkosten_data,
    sankey_data,
    stromen_per_kwartier,
    uurprofiel_data,
)


@pytest.fixture(scope="module")
def scenario():
    """Twee dagen met PV-overschot, tekort én DA-spread (batterij doet alles)."""
    n = 96 * 2
    ts = pd.date_range("2023-06-01", periods=n, freq="15min")
    uur = ts.hour + ts.minute / 60
    pv = np.clip(np.sin((uur - 6) / 12 * np.pi), 0, None) * 12.0    # tot 12 kWh/kwartier
    cons = np.full(n, 4.0)
    da = 80.0 + 60.0 * np.sin((uur - 18) / 24 * 2 * np.pi)          # avondpiek
    df = pd.DataFrame({"productie": pv, "consumptie": cons, "da_prijs": da}, index=ts)

    bat = BatteryParams(capacity_kwh=40.0, crate="1 op 2", dod=0.8,
                        efficiency=0.95, da_charging=False)
    tariff = tariff_simpel(cap_eur_kw_jaar=40.0, var_netkost_eur_kwh=0.02)
    res = simulate_lp(df, bat, tariff)
    return res, bat, tariff


class TestEnergiebehoud:
    def test_pv_volledig_verdeeld(self, scenario):
        res, bat, _ = scenario
        stromen = stromen_per_kwartier(res, bat)
        pv_uit = (stromen["pv_naar_verbruik"] + stromen["pv_naar_bess"]
                  + stromen["pv_naar_net"]).sum()
        assert pv_uit == pytest.approx(res["productie"].sum(), rel=1e-6)

    def test_import_volledig_verdeeld(self, scenario):
        res, bat, _ = scenario
        stromen = stromen_per_kwartier(res, bat)
        imp_uit = (stromen["net_naar_verbruik"] + stromen["net_naar_bess"]).sum()
        assert imp_uit == pytest.approx(res["g_imp"].sum(), rel=1e-6)

    def test_verbruik_volledig_gedekt(self, scenario):
        res, bat, _ = scenario
        stromen = stromen_per_kwartier(res, bat)
        dekking = (stromen["pv_naar_verbruik"] + stromen["bess_naar_verbruik"]
                   + stromen["net_naar_verbruik"]).sum()
        assert dekking == pytest.approx(res["consumptie"].sum(), rel=1e-6)

    def test_herkomstsplitsing_sommeert(self, scenario):
        res, bat, _ = scenario
        stromen = stromen_per_kwartier(res, bat)
        som = stromen["bess_naar_verbruik_uit_pv"] + stromen["bess_naar_verbruik_uit_net"]
        assert som.to_numpy() == pytest.approx(
            stromen["bess_naar_verbruik"].to_numpy(), abs=1e-9
        )

    def test_geen_negatieve_stromen(self, scenario):
        res, bat, _ = scenario
        stromen = stromen_per_kwartier(res, bat)
        for kolom in stromen.columns.drop("da_prijs"):
            assert (stromen[kolom] >= -1e-9).all(), f"negatief in {kolom}"


class TestSankey:
    def test_alleen_positieve_links(self, scenario):
        res, bat, _ = scenario
        sankey = sankey_data(stromen_per_kwartier(res, bat))
        assert all(l["mwh"] > 0 for l in sankey["links"])

    def test_verbruik_inflow_klopt(self, scenario):
        res, bat, _ = scenario
        sankey = sankey_data(stromen_per_kwartier(res, bat))
        inflow = sum(l["mwh"] for l in sankey["links"] if l["naar"] == "Verbruik")
        assert inflow == pytest.approx(res["consumptie"].sum() / 1000, abs=0.01)


class TestUurprofiel:
    def test_24_rijen(self, scenario):
        res, bat, _ = scenario
        profiel = uurprofiel_data(stromen_per_kwartier(res, bat))
        assert len(profiel) == 24
        assert [r["uur"] for r in profiel] == list(range(24))

    def test_stromen_dekken_verbruik(self, scenario):
        res, bat, _ = scenario
        profiel = uurprofiel_data(stromen_per_kwartier(res, bat))
        for rij in profiel:
            dekking = (rij["pv_naar_verbruik_kw"] + rij["bess_uit_pv_kw"]
                       + rij["bess_uit_net_kw"] + rij["net_naar_verbruik_kw"])
            assert dekking == pytest.approx(rij["consumptie_kw"], abs=0.1)


class TestMaandkosten:
    def test_besparing_consistent_met_vergelijking(self, scenario):
        res, bat, tariff = scenario
        maanden = maandkosten_data(res, tariff)
        som_besparing = sum(m["besparing_eur"] for m in maanden)

        df = res[["productie", "consumptie", "da_prijs"]]
        uit = vergelijk_zonder_met(df, bat, tariff)
        assert som_besparing == pytest.approx(uit["besparing_eur"], abs=0.05)

    def test_structuur(self, scenario):
        res, _, tariff = scenario
        maanden = maandkosten_data(res, tariff)
        assert len(maanden) == 1   # scenario valt binnen één maand
        m = maanden[0]
        assert m["maand"] == "2023-06"
        assert m["besparing_eur"] == pytest.approx(
            m["kost_zonder_eur"] - m["kost_met_eur"], abs=0.01
        )
