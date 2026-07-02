"""
Tests voor bess_api (FastAPI-laag). Gebruikt de fastapi TestClient — er hoeft
geen server te draaien. De simulatie-integratietest vereist de lokale
datacache (Synergrid + DA-prijzen) en wordt anders overgeslagen.
"""

import io

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from bess_api import app
from bess_prices import _cache_pad
from bess_profielen import _parsed_pad, _xlsb_pad

client = TestClient(app)

_CACHE_AANWEZIG = (
    (_parsed_pad(2026).exists() or _xlsb_pad(2026).exists())
    and _cache_pad(2026).exists()
)
needs_cache = pytest.mark.skipif(
    not _CACHE_AANWEZIG,
    reason="lokale datacache (Synergrid/ENTSO-E) ontbreekt",
)


def _meter_csv(n_dagen: int = 2) -> bytes:
    """Kleine geldige meterdata-CSV in het geheugen."""
    n = n_dagen * 96
    ts = pd.date_range("2024-03-01", periods=n, freq="15min")
    rng = np.random.default_rng(3)
    df = pd.DataFrame({
        "timestamp": ts,
        "afname":   4.0 + rng.random(n),
        "injectie": np.clip(rng.random(n) - 0.7, 0, None),
    })
    return df.to_csv(index=False).encode()


class TestHealth:
    def test_health(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


@needs_cache
class TestNetgebieden:
    def test_lijst_bevat_fluvius(self):
        r = client.get("/netgebieden")
        assert r.status_code == 200
        gebieden = r.json()["netgebieden"]
        assert any(g.startswith("Fluvius") for g in gebieden)


class TestValideer:
    def test_geldige_csv(self):
        r = client.post(
            "/valideer",
            files={"file": ("meter.csv", _meter_csv(), "text/csv")},
            data={"unit": "kWh"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "profiel_id" in body
        assert body["rapport"]["n_rows"] == 192
        assert body["jaarverbruik_kwh"] > 0

    def test_ongeldige_csv_geeft_400(self):
        r = client.post(
            "/valideer",
            files={"file": ("kapot.csv", b"kolom_a,kolom_b\n1,2\n", "text/csv")},
            data={"unit": "kWh"},
        )
        assert r.status_code == 400
        assert "kolommen" in r.json()["detail"].lower()

    def test_puntkomma_separator_autodetect(self):
        csv = _meter_csv().decode().replace(",", ";").encode()
        r = client.post(
            "/valideer",
            files={"file": ("fluvius.csv", csv, "text/csv")},
            data={"unit": "kWh"},
        )
        assert r.status_code == 200


class TestSimulatieValidatie:
    def test_onbekend_profiel_id_geeft_404(self):
        r = client.post("/simulatie", json={
            "profiel": {"type": "upload", "profiel_id": "bestaat-niet"},
            "batterij": {"capacity_kwh": 20},
        })
        assert r.status_code == 404

    def test_ongeldige_crate_geeft_400(self):
        r = client.post("/simulatie", json={
            "profiel": {"type": "standaard", "jaarverbruik_kwh": 10000},
            "batterij": {"capacity_kwh": 20, "crate": "1 op 99"},
        })
        assert r.status_code == 400
        assert "crate" in r.json()["detail"].lower()

    def test_negatief_jaarverbruik_geweigerd(self):
        r = client.post("/simulatie", json={
            "profiel": {"type": "standaard", "jaarverbruik_kwh": -5},
            "batterij": {"capacity_kwh": 20},
        })
        assert r.status_code == 422   # pydantic-validatie


@needs_cache
class TestSimulatieIntegratie:
    def test_standaard_profiel_2026(self):
        """Volledige keten: standaardprofiel zonder PV → backtest 2026 YTD."""
        r = client.post("/simulatie", json={
            "profiel": {"type": "standaard", "jaarverbruik_kwh": 20000, "kwp": 0},
            "batterij": {"capacity_kwh": 20},
            "jaren": [2026],
        })
        assert r.status_code == 200
        body = r.json()
        assert len(body["jaren"]) == 1
        rij = body["jaren"][0]
        assert rij["jaar"] == 2026
        assert rij["besparing_eur"] >= 0
        assert rij["kost_met_eur"] <= rij["kost_zonder_eur"]
        tvt = body["terugverdientijd"]
        assert tvt["investering_eur"] > 0
        assert "beperkingen" in body

    def test_kostenuitsplitsing_aanwezig(self):
        r = client.post("/simulatie", json={
            "profiel": {"type": "standaard", "jaarverbruik_kwh": 20000, "kwp": 0},
            "batterij": {"capacity_kwh": 20},
            "jaren": [2026],
        })
        rij = r.json()["jaren"][0]
        for kant in ("zonder", "met"):
            for comp in ("energie_eur", "netkost_var_eur", "capaciteit_eur",
                         "injectie_opbrengst_eur"):
                assert f"{kant}_{comp}" in rij
