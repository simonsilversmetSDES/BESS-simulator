"""
Day-ahead prijzen (België) ophalen via het ENTSO-E Transparency Platform.

Publieke API:
    laad_da_prijzen(start_jaar, eind_jaar) -> pd.Series   # cache-first, €/MWh
    haal_da_prijzen_jaar(jaar)             -> pd.Series   # één kalenderjaar

Resultaat: kwartierresolutie (uurprijzen forward-filled naar 15 min),
tz-aware Europe/Brussels index, waarden in €/MWh.

API-key: env var ENTSOE_API_KEY, of het bestand .entsoe_key in de projectroot
(staat in .gitignore — nooit committen).
"""

from __future__ import annotations

import os
import pathlib

import pandas as pd

_PROJECT_ROOT = pathlib.Path(__file__).parent
_CACHE_DIR = _PROJECT_ROOT / "data"
_KEY_FILE = _PROJECT_ROOT / ".entsoe_key"
_TZ = "Europe/Brussels"
_LAND = "BE"


def _lees_api_key(api_key: str | None = None) -> str:
    if api_key:
        return api_key
    env = os.environ.get("ENTSOE_API_KEY")
    if env:
        return env.strip()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "Geen ENTSO-E API-key gevonden. Zet ENTSOE_API_KEY of maak .entsoe_key aan."
    )


def _naar_kwartier(s: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """
    Zet een prijsreeks (uur- of kwartierresolutie) op het volledige
    15-min-raster [start, end). Uurprijzen worden forward-filled.
    """
    doel = pd.date_range(start, end, freq="15min", inclusive="left", tz=_TZ)
    s = s.sort_index()
    if s.index.tz is None:
        s.index = s.index.tz_localize(_TZ)
    else:
        s.index = s.index.tz_convert(_TZ)
    out = s.reindex(doel, method="ffill")
    # Eventuele gaten aan het begin (bv. reeks start later): backfill als noodgreep
    out = out.bfill()
    out.name = "da_prijs"
    return out


def _cache_pad(jaar: int) -> pathlib.Path:
    return _CACHE_DIR / f"da_{_LAND}_{jaar}.csv"


def _lees_cache(jaar: int) -> pd.Series | None:
    pad = _cache_pad(jaar)
    if not pad.exists():
        return None
    df = pd.read_csv(pad, index_col=0)
    # utc=True: gemengde +01:00/+02:00-offsets (zomer/wintertijd) correct parsen
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(_TZ)
    return df["da_prijs"]


def _schrijf_cache(jaar: int, s: pd.Series) -> None:
    _CACHE_DIR.mkdir(exist_ok=True)
    s.to_frame().to_csv(_cache_pad(jaar))


def haal_da_prijzen_jaar(
    jaar: int,
    api_key: str | None = None,
    refresh: bool = False,
) -> pd.Series:
    """
    DA-prijzen voor één kalenderjaar (België), kwartierresolutie, €/MWh.

    Cache-first: data/da_BE_<jaar>.csv wordt hergebruikt. Voor het lopende
    jaar wordt automatisch bijgehaald als de cache ouder is dan één dag.
    """
    vandaag = pd.Timestamp.now(tz=_TZ).normalize()
    start = pd.Timestamp(f"{jaar}-01-01", tz=_TZ)
    end = min(pd.Timestamp(f"{jaar + 1}-01-01", tz=_TZ), vandaag + pd.Timedelta(days=1))

    if start >= end:
        raise ValueError(f"Jaar {jaar} ligt in de toekomst.")

    cached = None if refresh else _lees_cache(jaar)
    if cached is not None:
        cache_actueel = cached.index.max() >= end - pd.Timedelta(minutes=15)
        if cache_actueel:
            return cached
        # lopend jaar: cache is verouderd → opnieuw ophalen

    from entsoe import EntsoePandasClient  # import hier: offline tests hoeven entsoe niet

    client = EntsoePandasClient(api_key=_lees_api_key(api_key))
    raw = client.query_day_ahead_prices(_LAND, start=start, end=end)
    if isinstance(raw, pd.DataFrame):
        raw = raw.iloc[:, 0]
    if raw.empty:
        raise RuntimeError(f"ENTSO-E gaf geen DA-prijzen terug voor {_LAND} {jaar}.")

    s = _naar_kwartier(raw, start, end)
    _schrijf_cache(jaar, s)
    return s


def laad_da_prijzen(
    start_jaar: int = 2022,
    eind_jaar: int | None = None,
    api_key: str | None = None,
) -> pd.Series:
    """
    DA-prijzen België van start_jaar t.e.m. eind_jaar (default: huidig jaar,
    year-to-date). Aaneengesloten reeks, kwartierresolutie, €/MWh.
    """
    if eind_jaar is None:
        eind_jaar = pd.Timestamp.now(tz=_TZ).year
    delen = [haal_da_prijzen_jaar(j, api_key=api_key) for j in range(start_jaar, eind_jaar + 1)]
    return pd.concat(delen)
