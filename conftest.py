import pytest
import pandas as pd


def _make_ts(n=8, start="2023-01-01 00:00", freq="15min") -> pd.Series:
    return pd.Series(pd.date_range(start, periods=n, freq=freq))


@pytest.fixture
def minimal_valid_df():
    """8 rijen, kWh/kwartier, geen PV."""
    ts = _make_ts(8)
    return pd.DataFrame({
        "timestamp": ts,
        "afname":   [10.5, 12.0, 8.3, 9.1, 11.2, 7.8, 10.0, 9.5],
        "injectie": [0.0,  0.0,  2.1, 1.5, 0.0,  3.2, 0.0,  0.5],
    })


@pytest.fixture
def valid_df_with_pv():
    """8 rijen, kWh/kwartier, met pv_productie."""
    ts = _make_ts(8)
    return pd.DataFrame({
        "timestamp":    ts,
        "afname":       [10.5, 12.0, 8.3, 9.1, 11.2, 7.8, 10.0, 9.5],
        "injectie":     [0.0,  0.0,  2.1, 1.5, 0.0,  3.2, 0.0,  0.5],
        "pv_productie": [0.0,  0.0,  5.0, 4.2, 2.0,  6.1, 1.0,  2.5],
    })


@pytest.fixture
def kw_input_df():
    """
    8 rijen met _kw-kolomsuffix — dekt de suffix-detectieroute in _detect_unit.
    Waarden representeren kW; na conversie ×0,25 → kWh/kwartier.
    """
    ts = _make_ts(8)
    return pd.DataFrame({
        "timestamp":  ts,
        "afname_kw":  [42.0, 48.0, 33.2, 36.4, 44.8, 31.2, 40.0, 38.0],
        "injectie_kw": [0.0,  0.0,  8.4,  6.0,  0.0, 12.8,  0.0,  2.0],
    })


@pytest.fixture
def df_with_gap():
    """8 rijen met een gat van 2 kwartieren (01:00 en 01:15 ontbreken)."""
    ts_before = pd.date_range("2023-01-01 00:00", periods=4, freq="15min")
    ts_after  = pd.date_range("2023-01-01 01:30", periods=4, freq="15min")
    ts = pd.Series(list(ts_before) + list(ts_after))
    return pd.DataFrame({
        "timestamp": ts,
        "afname":    [10.0] * 8,
        "injectie":  [1.0]  * 8,
    })


@pytest.fixture
def df_with_negatives():
    """8 rijen waarbij afname 2 negatieve waarden heeft (rij 1 en 5)."""
    ts = _make_ts(8)
    return pd.DataFrame({
        "timestamp": ts,
        "afname":    [10.5, -2.0, 8.3, 9.1, 11.2, -0.5, 10.0, 9.5],
        "injectie":  [0.0,   0.0, 2.1, 1.5,  0.0,  3.2,  0.0, 0.5],
    })


@pytest.fixture
def df_unsorted():
    """6 rijen met timestamps in omgekeerde volgorde."""
    ts = _make_ts(6)
    return pd.DataFrame({
        "timestamp": ts[::-1].reset_index(drop=True),
        "afname":    [10.0, 11.0, 9.0, 8.0, 12.0, 10.5],
        "injectie":  [0.0] * 6,
    })


@pytest.fixture
def tmp_csv(tmp_path, minimal_valid_df):
    """Schrijft minimal_valid_df naar een tijdelijk CSV-bestand."""
    p = tmp_path / "test_meter.csv"
    minimal_valid_df.to_csv(p, index=False)
    return p
