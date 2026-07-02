"""
PV-productieprofiel genereren via de PVGIS-API (Europese Commissie, JRC).

Publieke API:
    haal_pv_profiel(kwp, lat, lon, helling_graden, azimut_graden, jaar)
        -> pd.Series  (kWh per kwartier, tz-aware Europe/Brussels)

Azimut volgens PVGIS-conventie: 0 = zuid, -90 = oost, +90 = west.
Helling: 0 = plat, 90 = verticaal. Systeemverlies default 14 % (PVGIS-norm).

PVGIS is gratis en heeft geen API-key nodig. Data beschikbaar t.e.m. 2023
(SARAH3-stralingsdatabase). Resultaten worden lokaal gecachet in data/.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
import requests

_PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/seriescalc"
_RAD_DATABASE = "PVGIS-SARAH3"   # dekt 2005 t.e.m. 2023
_CACHE_DIR = pathlib.Path(__file__).parent / "data"
_TZ = "Europe/Brussels"

# Centrum België als default locatie
DEFAULT_LAT = 50.85
DEFAULT_LON = 4.35


def _uur_naar_kwartier(uur: pd.Series) -> pd.Series:
    """
    Zet een uurreeks vermogen (kW, tz-aware) om naar kWh per kwartier:
    elk uur wordt 4 kwartieren met elk P·0,25 kWh.
    """
    kwartier_index = pd.date_range(
        uur.index.min(),
        uur.index.max() + pd.Timedelta(minutes=45),
        freq="15min",
        tz=uur.index.tz,
    )
    kw = uur.reindex(kwartier_index, method="ffill")
    out = kw * 0.25
    out.name = "pv_kwh"
    return out


def _cache_pad(kwp, lat, lon, helling, azimut, jaar) -> pathlib.Path:
    naam = f"pvgis_{lat}_{lon}_{kwp}kwp_h{helling}_a{azimut}_{jaar}.csv"
    return _CACHE_DIR / naam.replace(" ", "")


def haal_pv_profiel(
    kwp: float,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    helling_graden: float = 35.0,
    azimut_graden: float = 0.0,
    jaar: int = 2023,
    systeemverlies_pct: float = 14.0,
    refresh: bool = False,
) -> pd.Series:
    """
    PV-productie in kWh per kwartier voor één kalenderjaar.

    kwp:             geïnstalleerd piekvermogen (kWp)
    helling_graden:  hellingshoek panelen (0 = plat, 35 = typisch schuin dak)
    azimut_graden:   oriëntatie (PVGIS: 0 = zuid, -90 = oost, +90 = west)
    jaar:            weerjaar uit de PVGIS-database (t.e.m. 2023)
    """
    pad = _cache_pad(kwp, lat, lon, helling_graden, azimut_graden, jaar)
    if pad.exists() and not refresh:
        df = pd.read_csv(pad, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(_TZ)
        return df["pv_kwh"]

    params = {
        "lat": lat,
        "lon": lon,
        "startyear": jaar,
        "endyear": jaar,
        "raddatabase": _RAD_DATABASE,
        "pvcalculation": 1,
        "peakpower": kwp,
        "loss": systeemverlies_pct,
        "angle": helling_graden,
        "aspect": azimut_graden,
        "outputformat": "json",
    }
    resp = requests.get(_PVGIS_URL, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    uurdata = payload["outputs"]["hourly"]
    # time-formaat: "20230101:0010" (UTC, minuutcomponent is een artefact)
    tijden = pd.to_datetime(
        [r["time"][:11] for r in uurdata], format="%Y%m%d:%H", utc=True
    )
    vermogen_kw = np.array([r["P"] for r in uurdata], dtype=float) / 1000.0

    uur = pd.Series(vermogen_kw, index=tijden).tz_convert(_TZ)
    kwartier = _uur_naar_kwartier(uur)

    _CACHE_DIR.mkdir(exist_ok=True)
    kwartier.to_frame().to_csv(pad)
    return kwartier
