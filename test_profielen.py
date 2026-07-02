"""
Tests voor bess_pv (uur→kwartier-conversie) en bess_profielen (Synergrid
RLP0N-schaling). De Synergrid-tests draaien alleen als de datacache aanwezig
is (data/ staat in .gitignore); de conversietests zijn altijd offline.
"""

import numpy as np
import pandas as pd
import pytest

from bess_pv import _uur_naar_kwartier
from bess_profielen import _parsed_pad, _xlsb_pad

_SYNERGRID_CACHE_AANWEZIG = _parsed_pad(2026).exists() or _xlsb_pad(2026).exists()
needs_synergrid = pytest.mark.skipif(
    not _SYNERGRID_CACHE_AANWEZIG,
    reason="Synergrid-cache ontbreekt (data/); vereist eenmalige download",
)


# ===========================================================================
# PVGIS uur→kwartier-conversie (offline)
# ===========================================================================

class TestUurNaarKwartier:
    def _uurreeks(self):
        idx = pd.date_range("2023-06-01 10:00", periods=3, freq="h",
                            tz="Europe/Brussels")
        return pd.Series([4.0, 8.0, 2.0], index=idx)   # kW

    def test_vier_kwartieren_per_uur(self):
        kw = _uur_naar_kwartier(self._uurreeks())
        assert len(kw) == 12

    def test_energie_behouden(self):
        uur = self._uurreeks()
        kw = _uur_naar_kwartier(uur)
        assert kw.sum() == pytest.approx(uur.sum())   # kW·1h == 4 × kW·0,25h

    def test_kwartierwaarde_is_kwart_van_uur(self):
        kw = _uur_naar_kwartier(self._uurreeks())
        assert kw.iloc[0:4].to_numpy() == pytest.approx([1.0] * 4)   # 4 kW → 1 kWh/kwartier
        assert kw.iloc[4:8].to_numpy() == pytest.approx([2.0] * 4)   # 8 kW → 2 kWh/kwartier

    def test_tz_behouden(self):
        kw = _uur_naar_kwartier(self._uurreeks())
        assert str(kw.index.tz) == "Europe/Brussels"


# ===========================================================================
# Synergrid RLP0N (vereist datacache)
# ===========================================================================

@needs_synergrid
class TestStandaardLastprofiel:
    def test_som_is_jaarverbruik(self):
        from bess_profielen import standaard_lastprofiel
        s = standaard_lastprofiel(25_000.0, "Fluvius Antwerpen", 2026)
        assert s.sum() == pytest.approx(25_000.0, rel=1e-6)

    def test_alle_fluvius_zones_aanwezig(self):
        from bess_profielen import beschikbare_netgebieden
        gebieden = beschikbare_netgebieden(2026)
        fluvius = [g for g in gebieden if g.lower().startswith("fluvius")]
        assert len(fluvius) == 8

    def test_onbekend_netgebied_geeft_fout(self):
        from bess_profielen import standaard_lastprofiel
        with pytest.raises(ValueError, match="Onbekend netgebied"):
            standaard_lastprofiel(10_000.0, "Bestaat Niet", 2026)

    def test_geen_negatieve_waarden(self):
        from bess_profielen import standaard_lastprofiel
        s = standaard_lastprofiel(10_000.0, "Fluvius Limburg", 2026)
        assert (s >= 0).all()

    def test_onbekend_jaar_geeft_fout(self):
        from bess_profielen import standaard_lastprofiel
        with pytest.raises(ValueError, match="Geen Synergrid-URL"):
            standaard_lastprofiel(10_000.0, "Fluvius Antwerpen", 2019)


@needs_synergrid
class TestMaakStandaardProfiel:
    def test_zonder_pv_productie_nul(self):
        from bess_profielen import maak_standaard_profiel
        df = maak_standaard_profiel(jaarverbruik_kwh=10_000, kwp=0.0)
        assert (df["productie"] == 0).all()
        assert df["consumptie"].sum() == pytest.approx(10_000.0, rel=1e-6)

    def test_kolommen_simulatieklaar(self):
        from bess_profielen import maak_standaard_profiel
        df = maak_standaard_profiel(jaarverbruik_kwh=10_000, kwp=0.0)
        assert {"productie", "consumptie"} <= set(df.columns)
        assert isinstance(df.index, pd.DatetimeIndex)
