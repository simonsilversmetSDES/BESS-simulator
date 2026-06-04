"""
Voorbeeld + smoke-test voor bess_core.

Draai: python test_bess.py
Vervang het synthetische blok door pd.read_csv(...) van echte meterdata
zodra je die hebt.
"""

import numpy as np
import pandas as pd
from bess_core import (
    BatteryParams, FinancialParams,
    reconstruct_profiles, simulate, summarize,
)


def make_synthetic_year(seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = 35040
    idx = pd.date_range("2023-01-01", periods=n, freq="15min")
    hour = idx.hour + idx.minute / 60
    doy = idx.dayofyear

    season = 0.4 + 0.6 * np.sin((doy - 80) / 365 * 2 * np.pi)
    daylight = np.clip(np.sin((hour - 6) / 12 * np.pi), 0, None)
    pv_kw = 600 * season * daylight * (0.8 + 0.4 * rng.random(n))

    base = 120 + 80 * np.clip(np.sin((hour - 6) / 14 * np.pi), 0, None)
    cons_kw = base * (0.85 + 0.3 * rng.random(n))

    prod = pv_kw * 0.25
    cons = cons_kw * 0.25
    net = prod - cons
    injectie = np.clip(net, 0, None)
    afname = np.clip(-net, 0, None)
    da = 80 + 60 * np.sin((hour - 18) / 24 * 2 * np.pi) + 20 * rng.standard_normal(n)

    return pd.DataFrame({
        "timestamp": idx, "afname": afname, "injectie": injectie,
        "pv_productie": prod, "da_prijs": da,
    })


def main():
    df = make_synthetic_year()
    df = reconstruct_profiles(df)

    bat = BatteryParams(capacity_kwh=600, crate="1 op 2", dod=0.8,
                        efficiency=0.95, da_charging=True)
    fin = FinancialParams()

    res = simulate(df, bat)
    out = summarize(res, bat, fin, energy_price_eur_mwh=180)

    # --- Sanity-checks ---
    assert res["level"].min() >= bat.floor_kwh - 1e-6, "Onder de DOD-floor gezakt"
    assert res["level"].max() <= bat.capacity_kwh + 1e-6, "Boven capaciteit geladen"
    assert out["energy"]["extra_ac_mwh"] > 0, "Batterij verhoogt autoconsumptie niet"
    assert out["financial"]["irr"] is not None or \
        out["financial"]["jaarbaten_eur"] <= 0, "IRR onverwacht None"

    print("Energie (MWh):")
    for k, v in out["energy"].items():
        print(f"  {k:18s} {v:10.2f}")
    print("\nFinancieel:")
    for k, v in out["financial"].items():
        print(f"  {k:30s} {v}")
    print("\nAlle sanity-checks geslaagd.")


if __name__ == "__main__":
    main()
