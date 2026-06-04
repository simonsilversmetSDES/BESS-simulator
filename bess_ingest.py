"""
Upload- en validatielaag voor kwartier-meterdata.

Publieke API:
    load_csv(path, unit='auto', **read_csv_kwargs) -> (DataFrame, IngestReport)
    validate(df, unit='auto')                       -> (DataFrame, IngestReport)

Eenheden: afname/injectie/pv_productie altijd in kWh/kwartier na validate().
"""

from __future__ import annotations

import pathlib
import re
import warnings
from dataclasses import dataclass, field

import pandas as pd


# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = {"timestamp", "afname", "injectie"}
_VALUE_COLUMNS = ["afname", "injectie", "pv_productie"]
RESOLUTION_MINUTES = 15
_KW_TO_KWH = 0.25          # kW × 0,25h = kWh per kwartier

_KW_SUFFIX = "_kw"
_KWH_SUFFIX = "_kwh"


# ---------------------------------------------------------------------------
# Rapport
# ---------------------------------------------------------------------------

@dataclass
class IngestReport:
    unit_input: str                        # 'kW' | 'kWh' | 'auto→kW' | 'auto→kWh'
    n_rows: int
    resolution_minutes: float
    date_range: tuple                      # (iso_start, iso_end)
    gaps: list                             # [{start, end, missing_steps}]
    negative_flags: dict                   # {col: [iso_timestamp, ...]}
    warnings: list                         # [str]
    errors: list                           # gereserveerd, altijd [] in v1


# ---------------------------------------------------------------------------
# Privé-helpers
# ---------------------------------------------------------------------------

def _strip_unit_suffixes(df: pd.DataFrame) -> tuple[pd.DataFrame, str | None]:
    """
    Zoekt _kw / _kwh suffixen op kolomnamen van waarde-kolommen.
    Hernoemt ze naar canonieke namen (afname, injectie, pv_productie).
    Retourneert (hernoemd_df, gedetecteerde_eenheid | None).
    Bij conflicterende suffixen: geen hernoeming, geeft None terug.
    """
    rename_map: dict[str, str] = {}
    found_units: set[str] = set()

    for col in df.columns:
        lower = col.lower()
        if lower.endswith(_KW_SUFFIX):
            canonical = col[: -len(_KW_SUFFIX)]
            if canonical in {"afname", "injectie", "pv_productie"}:
                rename_map[col] = canonical
                found_units.add("kW")
        elif lower.endswith(_KWH_SUFFIX):
            canonical = col[: -len(_KWH_SUFFIX)]
            if canonical in {"afname", "injectie", "pv_productie"}:
                rename_map[col] = canonical
                found_units.add("kWh")

    if not rename_map or len(found_units) > 1:
        return df, None

    detected = found_units.pop()
    return df.rename(columns=rename_map), detected


def _validate_columns(df: pd.DataFrame) -> None:
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Ontbrekende verplichte kolommen: {sorted(missing)}")


def _is_ambiguous_date_str(s: str) -> bool:
    """
    True als string een dd/mm of mm/dd patroon heeft waarbij beide componenten ≤ 12
    zijn en dus verwisselbaar. ISO-datums (YYYY-...) geven altijd False.
    """
    m = re.match(r"^(\d{1,2})[/\-\.](\d{1,2})[/\-\.]", s)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    return a <= 12 and b <= 12 and a != b


def _parse_timestamps(series: pd.Series) -> tuple[pd.Series, list[str]]:
    """
    Parseert de timestamp-kolom. Detecteert ambigue d/m-volgorde en waarschuwt.
    Retourneert (parsed_series, warnings).
    Gooit ValueError bij echt niet-parseerbare waarden.
    """
    warnings_out: list[str] = []

    def _to_dt(s, dayfirst):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return pd.to_datetime(s, errors="coerce", dayfirst=dayfirst)

    # Eerste poging: geen aanname over dag/maand-volgorde
    parsed = _to_dt(series, dayfirst=False)

    if parsed.isna().any():
        # Mislukt zonder dayfirst — probeer met dayfirst=True
        parsed_df = _to_dt(series, dayfirst=True)
        if parsed_df.isna().any():
            bad = int(parsed_df.isna().sum())
            raise ValueError(
                f"Niet-parseerbare timestamp-waarden ({bad} rij(en)). "
                "Controleer het datumformaat."
            )
        parsed = parsed_df
        # Controleer of de waarden die nu slagen ook ambigue waren
        raw_strs = series.astype(str)
        if raw_strs.apply(_is_ambiguous_date_str).any():
            warnings_out.append(
                "Timestamp-formaat ambigu (dd/mm of mm/dd); dayfirst=True aangenomen. "
                "Geef expliciet formaat mee via de date_format-parameter van load_csv()."
            )
        # else: dag > 12 → ondubbelzinnig dd/mm, geen warning nodig
    else:
        # Alles geparseerd — controleer op ambiguïteit in de rauwe strings
        raw_strs = series.astype(str)
        if raw_strs.apply(_is_ambiguous_date_str).any():
            warnings_out.append(
                "Timestamp-formaat ambigu (dd/mm of mm/dd); dayfirst=True aangenomen. "
                "Geef expliciet formaat mee via de date_format-parameter van load_csv()."
            )
            parsed = _to_dt(series, dayfirst=True)

    return parsed, warnings_out


def _validate_resolution(ts: pd.Series) -> tuple[float, list[str]]:
    """
    Controleert of de tijdresolutie 15 min is.
    Gaten (meervouden van 15 min) zijn toegestaan — die worden apart gedetecteerd.
    Gooit ValueError bij verkeerde of niet-uniforme stapgrootte.
    Retourneert (resolutie_minuten, warnings).
    """
    if len(ts) < 2:
        raise ValueError("Te weinig rijen om tijdresolutie te bepalen (minimum 2).")

    diffs_sec = ts.diff().dropna().dt.total_seconds()
    pos = diffs_sec[diffs_sec > 0]

    if pos.empty:
        raise ValueError("Alle tijdstappen zijn nul of negatief.")

    min_step_sec = pos.min()
    min_step_min = min_step_sec / 60

    if abs(min_step_min - RESOLUTION_MINUTES) > 0.1:
        raise ValueError(
            f"Tijdresolutie is {min_step_min:.1f} min, verwacht {RESOLUTION_MINUTES} min."
        )

    # Alle positieve stappen moeten veelvoud zijn van 15 min (anders: sensorafwijking)
    remainders = pos % (RESOLUTION_MINUTES * 60)
    if (remainders > 6).any():            # 6-seconde tolerantie
        raise ValueError(
            f"Niet-uniforme tijdstappen: sommige stappen zijn geen veelvoud van "
            f"{RESOLUTION_MINUTES} min."
        )

    return min_step_min, []


def _detect_gaps(ts: pd.Series) -> list[dict]:
    """Groepeert aaneengesloten missende 15-min stappen tot gat-objecten."""
    expected = pd.date_range(ts.min(), ts.max(), freq="15min")
    missing = expected.difference(ts)
    if missing.empty:
        return []

    gaps: list[dict] = []
    block_start = missing[0]
    prev = missing[0]
    for m in missing[1:]:
        if (m - prev).total_seconds() > 15 * 60:
            gaps.append({
                "start": block_start.isoformat(),
                "end": prev.isoformat(),
                "missing_steps": int((prev - block_start).total_seconds() / 900) + 1,
            })
            block_start = m
        prev = m
    gaps.append({
        "start": block_start.isoformat(),
        "end": prev.isoformat(),
        "missing_steps": int((prev - block_start).total_seconds() / 900) + 1,
    })
    return gaps


def _flag_negatives(df: pd.DataFrame) -> dict[str, list[str]]:
    """Retourneert {kolom: [iso_timestamps]} voor kolommen met negatieve waarden."""
    flags: dict[str, list[str]] = {}
    check_cols = [c for c in _VALUE_COLUMNS if c in df.columns]
    for col in check_cols:
        mask = df[col] < 0
        if mask.any():
            flags[col] = df.loc[mask, "timestamp"].dt.strftime("%Y-%m-%dT%H:%M").tolist()
    return flags


def _detect_unit(
    df: pd.DataFrame, unit: str, suffix_unit: str | None
) -> tuple[str, list[str]]:
    """
    Retourneert (gedetecteerde_eenheid, warnings).
    Prioriteit: expliciete arg > kolomnaam-suffix > mediaan-heuristiek (altijd onzeker).
    """
    if unit not in ("auto", "kW", "kWh"):
        raise ValueError(
            f"Ongeldige unit-waarde: '{unit}'. Kies 'kW', 'kWh' of 'auto'."
        )
    if unit in ("kW", "kWh"):
        return unit, []

    # Suffix-detectie: geeft zekerheid, geen warning
    if suffix_unit is not None:
        return suffix_unit, []

    # Mediaan-heuristiek: altijd onzeker → altijd waarschuwen
    median_val = (df["afname"] + df["injectie"]).median()
    detected = "kW" if median_val > 50 else "kWh"
    return detected, [
        f"Eenheid auto-gedetecteerd als '{detected}' op basis van mediaan "
        f"(mediaan afname+injectie = {median_val:.1f}), maar dit is onbetrouwbaar "
        "voor grote installaties. Geef unit expliciet op via "
        "load_csv(..., unit='kW') of unit='kWh'."
    ]


def _convert_units(df: pd.DataFrame, detected_unit: str) -> pd.DataFrame:
    """Vermenigvuldigt waarde-kolommen met 0,25 als eenheid kW is."""
    out = df.copy()
    if detected_unit == "kW":
        cols = [c for c in _VALUE_COLUMNS if c in out.columns]
        out[cols] = out[cols] * _KW_TO_KWH
    return out


# ---------------------------------------------------------------------------
# Publieke API
# ---------------------------------------------------------------------------

def validate(
    df: pd.DataFrame,
    unit: str = "auto",
) -> tuple[pd.DataFrame, IngestReport]:
    """
    Valideert en normaliseert een DataFrame met kwartier-meterdata.

    Blocking fouten → raise ValueError (ontbrekende kolommen, verkeerde resolutie).
    Niet-blokkerende bevindingen → IngestReport.warnings.

    Retourneert (schone_df, rapport) waarbij schone_df:
    - timestamp als datetime, oplopend gesorteerd
    - afname, injectie (en optioneel pv_productie) in kWh/kwartier
    """
    warnings_acc: list[str] = []
    work = df.copy()

    # Kolomnamen normaliseren: spaties verwijderen
    work.columns = work.columns.str.strip()

    # Suffix-detectie en hernoeming vóór kolomvalidatie
    work, suffix_unit = _strip_unit_suffixes(work)

    # Verplichte kolommen
    _validate_columns(work)

    # Timestamps parsen
    ts_parsed, ts_warnings = _parse_timestamps(work["timestamp"])
    work["timestamp"] = ts_parsed
    warnings_acc.extend(ts_warnings)

    # Sorteren indien niet oplopend
    if not work["timestamp"].is_monotonic_increasing:
        work = work.sort_values("timestamp").reset_index(drop=True)
        warnings_acc.append("Timestamps niet oplopend — gesorteerd op timestamp.")

    # Resolutie valideren
    res_min, res_warnings = _validate_resolution(work["timestamp"])
    warnings_acc.extend(res_warnings)

    # Eenheidsdetectie
    detected_unit, unit_warnings = _detect_unit(work, unit, suffix_unit)
    warnings_acc.extend(unit_warnings)

    # Negatieve waarden flaggen (vóór conversie, in originele schaal)
    negative_flags = _flag_negatives(work)
    for col, timestamps in negative_flags.items():
        warnings_acc.append(
            f"Kolom '{col}' heeft {len(timestamps)} negatieve waarde(n). "
            "Worden geclamped door reconstruct_profiles()."
        )

    # Gapdetectie
    gaps = _detect_gaps(work["timestamp"])
    if gaps:
        total_missing = sum(g["missing_steps"] for g in gaps)
        warnings_acc.append(
            f"{len(gaps)} gat(en) in tijdreeks "
            f"(totaal {total_missing} ontbrekende kwartieren)."
        )

    # Eenheidsconversie
    out = _convert_units(work, detected_unit)

    unit_label = f"auto→{detected_unit}" if unit == "auto" else detected_unit

    report = IngestReport(
        unit_input=unit_label,
        n_rows=len(out),
        resolution_minutes=res_min,
        date_range=(
            out["timestamp"].min().isoformat(),
            out["timestamp"].max().isoformat(),
        ),
        gaps=gaps,
        negative_flags=negative_flags,
        warnings=warnings_acc,
        errors=[],
    )
    return out, report


def load_csv(
    path: str | pathlib.Path,
    unit: str = "auto",
    **read_csv_kwargs,
) -> tuple[pd.DataFrame, IngestReport]:
    """
    Leest een CSV-bestand in en valideert het via validate().

    path: pad naar het CSV-bestand.
    unit: 'auto' | 'kW' | 'kWh'
    **read_csv_kwargs: doorgegeven aan pd.read_csv()
        (bv. sep=';', decimal=',', encoding='utf-8-sig').

    Gooit FileNotFoundError als het bestand niet bestaat.
    Gooit ValueError bij blokkerende validatiefouten.
    """
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"CSV-bestand niet gevonden: {path}")
    raw = pd.read_csv(path, **read_csv_kwargs)
    raw.columns = raw.columns.str.strip()
    return validate(raw, unit=unit)
