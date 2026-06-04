"""
Pytest-suite voor bess_ingest.py.

Groepen:
  A: Kolomvalidatie
  B: Eenheiddetectie en -conversie
  C: Tijdstempelvalidatie
  D: Gapdetectie
  E: Negatieve waarden
  F: load_csv integratie
  G: Energiebalans sanity-check
  H: IngestReport structuur
"""

import json

import pandas as pd
import pytest

from bess_core import reconstruct_profiles
from bess_ingest import IngestReport, load_csv, validate


def _make_ts(n=8, start="2023-01-01 00:00", freq="15min") -> pd.Series:
    return pd.Series(pd.date_range(start, periods=n, freq=freq))


# ===========================================================================
# A: Kolomvalidatie
# ===========================================================================

class TestColumnValidation:
    def test_missing_afname_raises(self, minimal_valid_df):
        with pytest.raises(ValueError, match="afname"):
            validate(minimal_valid_df.drop(columns="afname"))

    def test_missing_injectie_raises(self, minimal_valid_df):
        with pytest.raises(ValueError, match="injectie"):
            validate(minimal_valid_df.drop(columns="injectie"))

    def test_missing_timestamp_raises(self, minimal_valid_df):
        with pytest.raises(ValueError, match="timestamp"):
            validate(minimal_valid_df.drop(columns="timestamp"))

    def test_missing_both_required_raises(self):
        df = pd.DataFrame({"timestamp": _make_ts(4), "pv_productie": [1.0] * 4})
        with pytest.raises(ValueError):
            validate(df)

    def test_optional_pv_absent_ok(self, minimal_valid_df):
        out, _ = validate(minimal_valid_df, unit="kWh")
        assert "pv_productie" not in out.columns

    def test_optional_pv_present_ok(self, valid_df_with_pv):
        out, _ = validate(valid_df_with_pv, unit="kWh")
        assert "pv_productie" in out.columns

    def test_extra_columns_preserved(self, minimal_valid_df):
        df = minimal_valid_df.copy()
        df["meter_id"] = "ABC"
        out, _ = validate(df, unit="kWh")
        assert "meter_id" in out.columns


# ===========================================================================
# B: Eenheiddetectie en -conversie
# ===========================================================================

class TestUnitDetection:
    def test_explicit_kw_converts(self, minimal_valid_df):
        orig = minimal_valid_df["afname"].to_numpy()
        out, report = validate(minimal_valid_df, unit="kW")
        assert report.unit_input == "kW"
        assert out["afname"].to_numpy() == pytest.approx(orig * 0.25)

    def test_explicit_kw_no_uncertainty_warning(self, minimal_valid_df):
        _, report = validate(minimal_valid_df, unit="kW")
        assert not any("onbetrouwbaar" in w for w in report.warnings)

    def test_explicit_kwh_no_conversion(self, minimal_valid_df):
        orig = minimal_valid_df["afname"].tolist()
        out, report = validate(minimal_valid_df, unit="kWh")
        assert report.unit_input == "kWh"
        assert out["afname"].tolist() == pytest.approx(orig)

    def test_auto_suffix_kw_detects_kw(self, kw_input_df):
        _, report = validate(kw_input_df, unit="auto")
        assert report.unit_input == "auto→kW"

    def test_auto_suffix_kw_no_uncertainty_warning(self, kw_input_df):
        _, report = validate(kw_input_df, unit="auto")
        assert not any("onbetrouwbaar" in w for w in report.warnings)

    def test_auto_suffix_kwh_detects_kwh(self):
        ts = _make_ts(6)
        df = pd.DataFrame({
            "timestamp":    ts,
            "afname_kwh":   [10.0] * 6,
            "injectie_kwh": [0.0]  * 6,
        })
        _, report = validate(df, unit="auto")
        assert report.unit_input == "auto→kWh"
        assert not any("onbetrouwbaar" in w for w in report.warnings)

    def test_auto_median_always_warns(self, minimal_valid_df):
        # Geen suffix → mediaan-heuristiek → altijd onzekerheids-warning
        _, report = validate(minimal_valid_df, unit="auto")
        assert any("onbetrouwbaar" in w for w in report.warnings)

    def test_conversion_factor_exact(self):
        ts = _make_ts(4)
        df = pd.DataFrame({
            "timestamp": ts,
            "afname":    [4.0] * 4,
            "injectie":  [0.0] * 4,
        })
        out, _ = validate(df, unit="kW")
        assert out["afname"].tolist() == pytest.approx([1.0] * 4)

    def test_conversion_includes_pv(self, valid_df_with_pv):
        orig_pv = valid_df_with_pv["pv_productie"].tolist()
        out, _ = validate(valid_df_with_pv, unit="kW")
        assert out["pv_productie"].tolist() == pytest.approx([v * 0.25 for v in orig_pv])

    def test_invalid_unit_raises(self, minimal_valid_df):
        with pytest.raises(ValueError, match="unit"):
            validate(minimal_valid_df, unit="percent")

    def test_auto_unit_input_has_prefix(self, kw_input_df):
        _, report = validate(kw_input_df, unit="auto")
        assert report.unit_input.startswith("auto→")


# ===========================================================================
# C: Tijdstempelvalidatie
# ===========================================================================

class TestTimestampValidation:
    def test_valid_15min_resolution(self, minimal_valid_df):
        _, report = validate(minimal_valid_df, unit="kWh")
        assert report.resolution_minutes == pytest.approx(15.0)

    def test_30min_resolution_raises(self):
        ts = _make_ts(6, freq="30min")
        df = pd.DataFrame({"timestamp": ts, "afname": [1.0] * 6, "injectie": [0.0] * 6})
        with pytest.raises(ValueError, match="15"):
            validate(df)

    def test_non_uniform_raises(self):
        # 20-min stappen — geen veelvoud van 15 min
        ts = pd.Series([
            pd.Timestamp("2023-01-01 00:00"),
            pd.Timestamp("2023-01-01 00:20"),
            pd.Timestamp("2023-01-01 00:40"),
            pd.Timestamp("2023-01-01 01:00"),
        ])
        df = pd.DataFrame({"timestamp": ts, "afname": [1.0] * 4, "injectie": [0.0] * 4})
        with pytest.raises(ValueError):
            validate(df)

    def test_unsorted_sorted_with_warning(self, df_unsorted):
        out, report = validate(df_unsorted, unit="kWh")
        assert out["timestamp"].is_monotonic_increasing
        assert any("oplopend" in w or "gesorteerd" in w for w in report.warnings)

    def test_unparseable_timestamp_raises(self):
        df = pd.DataFrame({
            "timestamp": ["geen-datum", "ook-niet", "nope"],
            "afname":    [1.0] * 3,
            "injectie":  [0.0] * 3,
        })
        with pytest.raises(ValueError):
            validate(df)

    def test_iso_string_no_ambiguity_warning(self, minimal_valid_df):
        df = minimal_valid_df.copy()
        df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
        _, report = validate(df, unit="kWh")
        assert not any("ambigu" in w for w in report.warnings)

    def test_ambiguous_date_string_warns(self):
        # "05/06/2023" — dag=5 en maand=6 zijn beide ≤ 12, volgorde onduidelijk
        strs = [f"05/06/2023 {h:02d}:{m:02d}" for h, m in [(0,0),(0,15),(0,30),(0,45)]]
        df = pd.DataFrame({"timestamp": strs, "afname": [1.0]*4, "injectie": [0.0]*4})
        _, report = validate(df, unit="kWh")
        assert any("ambigu" in w for w in report.warnings)

    def test_unambiguous_day_first_no_warning(self):
        # "31/01/2023" — dag=31 > 12, dus ondubbelzinnig dd/mm
        strs = [f"31/01/2023 {h:02d}:{m:02d}" for h, m in [(0,0),(0,15),(0,30),(0,45)]]
        df = pd.DataFrame({"timestamp": strs, "afname": [1.0]*4, "injectie": [0.0]*4})
        _, report = validate(df, unit="kWh")
        assert not any("ambigu" in w for w in report.warnings)

    def test_single_row_raises(self):
        df = pd.DataFrame({
            "timestamp": [pd.Timestamp("2023-01-01")],
            "afname":    [1.0],
            "injectie":  [0.0],
        })
        with pytest.raises(ValueError):
            validate(df)

    def test_date_range_correct(self, minimal_valid_df):
        _, report = validate(minimal_valid_df, unit="kWh")
        assert report.date_range[0].startswith("2023-01-01T00:00")
        assert report.date_range[1].startswith("2023-01-01T01:45")


# ===========================================================================
# D: Gapdetectie
# ===========================================================================

class TestGapDetection:
    def test_no_gaps_empty_list(self, minimal_valid_df):
        _, report = validate(minimal_valid_df, unit="kWh")
        assert report.gaps == []

    def test_single_gap_detected(self, df_with_gap):
        _, report = validate(df_with_gap, unit="kWh")
        assert len(report.gaps) == 1

    def test_gap_missing_steps(self, df_with_gap):
        _, report = validate(df_with_gap, unit="kWh")
        assert report.gaps[0]["missing_steps"] == 2

    def test_gap_start_end_correct(self, df_with_gap):
        _, report = validate(df_with_gap, unit="kWh")
        assert "2023-01-01T01:00" in report.gaps[0]["start"]
        assert "2023-01-01T01:15" in report.gaps[0]["end"]

    def test_multiple_gaps_detected(self):
        ts = pd.Series([
            pd.Timestamp("2023-01-01 00:00"),
            pd.Timestamp("2023-01-01 00:15"),
            pd.Timestamp("2023-01-01 01:00"),   # gap: 00:30 en 00:45
            pd.Timestamp("2023-01-01 01:15"),
            pd.Timestamp("2023-01-01 02:00"),   # gap: 01:30 en 01:45
            pd.Timestamp("2023-01-01 02:15"),
        ])
        df = pd.DataFrame({"timestamp": ts, "afname": [1.0]*6, "injectie": [0.0]*6})
        _, report = validate(df, unit="kWh")
        assert len(report.gaps) == 2

    def test_gap_warning_in_report(self, df_with_gap):
        _, report = validate(df_with_gap, unit="kWh")
        assert any("gat" in w.lower() for w in report.warnings)

    def test_n_rows_is_actual(self, df_with_gap):
        _, report = validate(df_with_gap, unit="kWh")
        assert report.n_rows == len(df_with_gap)


# ===========================================================================
# E: Negatieve waarden
# ===========================================================================

class TestNegativeValues:
    def test_negative_afname_flagged(self, df_with_negatives):
        _, report = validate(df_with_negatives, unit="kWh")
        assert "afname" in report.negative_flags
        assert len(report.negative_flags["afname"]) == 2

    def test_negative_injectie_flagged(self, minimal_valid_df):
        df = minimal_valid_df.copy()
        df.loc[0, "injectie"] = -1.0
        _, report = validate(df, unit="kWh")
        assert "injectie" in report.negative_flags

    def test_negative_pv_flagged(self, valid_df_with_pv):
        df = valid_df_with_pv.copy()
        df.loc[2, "pv_productie"] = -0.5
        _, report = validate(df, unit="kWh")
        assert "pv_productie" in report.negative_flags

    def test_no_negatives_empty_dict(self, minimal_valid_df):
        _, report = validate(minimal_valid_df, unit="kWh")
        assert report.negative_flags == {}

    def test_negatives_do_not_raise(self, df_with_negatives):
        out, _ = validate(df_with_negatives, unit="kWh")
        assert out is not None

    def test_negatives_warning_in_report(self, df_with_negatives):
        _, report = validate(df_with_negatives, unit="kWh")
        assert any("negatieve" in w.lower() for w in report.warnings)

    def test_negative_timestamp_correct(self, df_with_negatives):
        # Rij 1 (index 1) = 2023-01-01 00:15
        _, report = validate(df_with_negatives, unit="kWh")
        assert "2023-01-01T00:15" in report.negative_flags["afname"]

    def test_negatives_flagged_before_kw_conversion(self):
        # _kw suffix → suffix-detectie → eenheid kW → conversie na flagging
        ts = _make_ts(4)
        df = pd.DataFrame({
            "timestamp":   ts,
            "afname_kw":   [-120.0, 80.0, 90.0, 100.0],
            "injectie_kw": [0.0] * 4,
        })
        _, report = validate(df, unit="auto")
        assert "afname" in report.negative_flags


# ===========================================================================
# F: load_csv integratie
# ===========================================================================

class TestLoadCsv:
    def test_valid_file(self, tmp_csv):
        out, report = load_csv(tmp_csv, unit="kWh")
        assert isinstance(out, pd.DataFrame)
        assert isinstance(report, IngestReport)
        assert not report.errors

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_csv(tmp_path / "bestaat_niet.csv", unit="kWh")

    def test_semicolon_separator(self, tmp_path, minimal_valid_df):
        p = tmp_path / "semi.csv"
        minimal_valid_df.to_csv(p, index=False, sep=";")
        out, _ = load_csv(p, unit="kWh", sep=";")
        assert "afname" in out.columns

    def test_result_matches_validate(self, tmp_csv, minimal_valid_df):
        out_csv, _ = load_csv(tmp_csv, unit="kWh")
        out_val, _ = validate(minimal_valid_df, unit="kWh")
        pd.testing.assert_frame_equal(
            out_csv.reset_index(drop=True),
            out_val.reset_index(drop=True),
        )


# ===========================================================================
# G: Energiebalans sanity-check
# ===========================================================================

class TestEnergyBalanceSanityCheck:
    def test_reconstructed_consumptie_not_structurally_negative(self, valid_df_with_pv):
        """
        Na validatie mag de ruwe gereconstrueerde consumptie (afname + productie - injectie)
        niet structureel negatief zijn. Meer dan 1% negatief duidt op een tekenfout in de input.
        """
        out, _ = validate(valid_df_with_pv, unit="kWh")
        # Reken handmatig zonder clamping (reconstruct_profiles clipt al)
        raw_consumptie = out["afname"] + out["pv_productie"] - out["injectie"]
        neg_fraction = (raw_consumptie < -0.01).mean()
        assert neg_fraction < 0.01, (
            f"Consumptie is structureel negatief ({neg_fraction:.1%} van rijen). "
            "Controleer tekens in afname/injectie/pv_productie."
        )


# ===========================================================================
# H: IngestReport structuur
# ===========================================================================

class TestIngestReportStructure:
    def test_is_dataclass_instance(self, minimal_valid_df):
        _, report = validate(minimal_valid_df, unit="kWh")
        assert isinstance(report, IngestReport)

    def test_gaps_json_serialisable(self, df_with_gap):
        _, report = validate(df_with_gap, unit="kWh")
        json.dumps(report.gaps)   # mag niet gooien

    def test_auto_unit_input_has_prefix(self, kw_input_df):
        _, report = validate(kw_input_df, unit="auto")
        assert report.unit_input.startswith("auto→")
