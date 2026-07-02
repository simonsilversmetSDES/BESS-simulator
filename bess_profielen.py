"""
Standaard verbruiksprofielen (Synergrid RLP0N) + PV-profiel (PVGIS) combineren
tot een simulatieklaar jaarprofiel — voor gebruikers zonder eigen meterdata.

Publieke API:
    beschikbare_netgebieden(jaar)                  -> list[str]
    standaard_lastprofiel(jaarverbruik_kwh, ...)   -> pd.Series  (kWh/kwartier)
    maak_standaard_profiel(jaarverbruik_kwh, kwp, ...) -> pd.DataFrame
        met kolommen 'productie' en 'consumptie', klaar voor
        vergelijk_zonder_met() of backtest_jaren().

Bron lastprofielen: Synergrid "RLP0N Electricity all DSOs" — de officiële
reële lastprofielen per netgebied (fracties van het jaarverbruik per
kwartier, som = 1). Wordt automatisch gedownload en gecachet in data/.
"""

from __future__ import annotations

import pathlib
import urllib.parse

import numpy as np
import pandas as pd
import requests

from bess_pv import DEFAULT_LAT, DEFAULT_LON, haal_pv_profiel

_CACHE_DIR = pathlib.Path(__file__).parent / "data"

_RLP_URLS = {
    2026: "https://www.synergrid.be/images/downloads/SLP-RLP-SPP/2026/RLP0N%202026%20Electricity%20all%20DSOs.xlsb",
    2025: "https://www.synergrid.be/images/downloads/RLP0N%202025%20Electricity%20all%20DSOs.xlsb",
}
_META_KOLOMMEN = ["CET", "Year", "Month", "Day", "h", "Min", "Date"]

DEFAULT_NETGEBIED = "Fluvius Antwerpen"


def _xlsb_pad(jaar: int) -> pathlib.Path:
    return _CACHE_DIR / f"rlp0n_{jaar}_all_dsos.xlsb"


def _parsed_pad(jaar: int) -> pathlib.Path:
    return _CACHE_DIR / f"rlp0n_{jaar}_parsed.csv"


def _download_rlp(jaar: int) -> pathlib.Path:
    if jaar not in _RLP_URLS:
        raise ValueError(
            f"Geen Synergrid-URL bekend voor jaar {jaar}. "
            f"Beschikbaar: {sorted(_RLP_URLS)}."
        )
    pad = _xlsb_pad(jaar)
    if pad.exists():
        return pad
    _CACHE_DIR.mkdir(exist_ok=True)
    resp = requests.get(_RLP_URLS[jaar], timeout=300)
    resp.raise_for_status()
    pad.write_bytes(resp.content)
    return pad


def _laad_rlp_tabel(jaar: int) -> pd.DataFrame:
    """
    Laadt de RLP0N-fracties voor alle netgebieden als DataFrame met
    DatetimeIndex (lokale tijd) en één kolom per netgebied. Cache-first:
    het trage xlsb-parsen gebeurt maar één keer per jaar.
    """
    parsed = _parsed_pad(jaar)
    if parsed.exists():
        df = pd.read_csv(parsed, index_col=0, parse_dates=True)
        return df

    xlsb = _download_rlp(jaar)
    raw = pd.read_excel(xlsb, engine="pyxlsb", sheet_name="RLP96UbyDGO")

    # Rij 0 = DGO-namen, rij 1 = EAN-codes; data start op rij 2.
    # Kolomnamen komen uit de bestandsheader ('FLUVIUS ANTWERPEN', ...).
    data = raw.iloc[2:].copy()
    data.columns = [str(c).strip() for c in raw.columns]

    kolom_map = dict(zip(data.columns[: len(_META_KOLOMMEN)], _META_KOLOMMEN))
    data = data.rename(columns=kolom_map)

    for c in ["Year", "Month", "Day", "h", "Min"]:
        data[c] = pd.to_numeric(data[c], errors="coerce")
    data = data.dropna(subset=["Year", "Month", "Day", "h", "Min"])

    idx = pd.to_datetime(dict(
        year=data["Year"].astype(int),
        month=data["Month"].astype(int),
        day=data["Day"].astype(int),
        hour=data["h"].astype(int),
        minute=data["Min"].astype(int),
    ))

    dso_kolommen = [c for c in data.columns if c not in _META_KOLOMMEN
                    and not c.startswith("Unnamed")]
    out = data[dso_kolommen].apply(pd.to_numeric, errors="coerce")
    out.index = idx
    # Nette weergavenamen uit de bestandsheader ("FLUVIUS ANTWERPEN" → titel)
    out.columns = [c.title() if c.isupper() else c for c in out.columns]

    out.to_csv(parsed)
    return out


def beschikbare_netgebieden(jaar: int = 2026) -> list[str]:
    """Alle netgebieden (DSO-zones) in het Synergrid-bestand voor dat jaar."""
    return list(_laad_rlp_tabel(jaar).columns)


def standaard_lastprofiel(
    jaarverbruik_kwh: float,
    netgebied: str = DEFAULT_NETGEBIED,
    jaar: int = 2026,
) -> pd.Series:
    """
    Verbruik in kWh per kwartier volgens het officiële RLP0N-lastprofiel,
    geschaald naar het opgegeven jaarverbruik.
    """
    tabel = _laad_rlp_tabel(jaar)
    if netgebied not in tabel.columns:
        raise ValueError(
            f"Onbekend netgebied '{netgebied}'. "
            f"Kies uit: {list(tabel.columns)}"
        )
    fracties = tabel[netgebied]
    s = fracties * jaarverbruik_kwh
    s.name = "consumptie"
    return s


def maak_standaard_profiel(
    jaarverbruik_kwh: float,
    kwp: float = 0.0,
    netgebied: str = DEFAULT_NETGEBIED,
    profiel_jaar: int = 2026,
    helling_graden: float = 35.0,
    azimut_graden: float = 0.0,
    lat: float = DEFAULT_LAT,
    lon: float = DEFAULT_LON,
    pv_weerjaar: int = 2023,
) -> pd.DataFrame:
    """
    Simulatieklaar jaarprofiel zonder eigen meterdata:
    consumptie = Synergrid RLP0N × jaarverbruik; productie = PVGIS-profiel.

    Het PV-profiel (weerjaar t.e.m. 2023) wordt per kalenderdag+tijdstip op de
    kalender van het profiel_jaar gelegd (29 februari → waarden 28 februari).

    Retourneert DataFrame met kolommen 'productie', 'consumptie' (kWh/kwartier)
    en een DatetimeIndex — direct bruikbaar in vergelijk_zonder_met() en
    backtest_jaren().
    """
    cons = standaard_lastprofiel(jaarverbruik_kwh, netgebied, profiel_jaar)

    if kwp > 0:
        pv = haal_pv_profiel(
            kwp=kwp, lat=lat, lon=lon,
            helling_graden=helling_graden, azimut_graden=azimut_graden,
            jaar=pv_weerjaar,
        )
        pv_naief = pv.copy()
        pv_naief.index = pv_naief.index.tz_localize(None)
        lookup = dict(zip(pv_naief.index.strftime("%m-%d %H:%M"), pv_naief.to_numpy()))

        keys = cons.index.strftime("%m-%d %H:%M")
        prod = np.array([
            lookup.get(k, lookup.get("02-28" + k[5:], 0.0)) if k.startswith("02-29")
            else lookup.get(k, 0.0)
            for k in keys
        ])
    else:
        prod = np.zeros(len(cons))

    return pd.DataFrame(
        {"productie": prod, "consumptie": cons.to_numpy()},
        index=cons.index,
    )
