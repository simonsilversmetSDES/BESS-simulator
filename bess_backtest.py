"""
Backtest: één jaarprofiel (verbruik + PV) doorrekenen tegen de werkelijke
Belgische DA-prijzen van meerdere jaren (2022 t.e.m. heden).

Publieke API:
    backtest_jaren(profiel_df, battery, tariff, jaren) -> pd.DataFrame

Het profiel wordt per kalenderdag+tijdstip op elk prijsjaar gelegd
(29 februari krijgt de waarden van 28 februari; het lopende jaar wordt
year-to-date doorgerekend).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bess_core import BatteryParams, TariffParams, vergelijk_zonder_met
from bess_prices import haal_da_prijzen_jaar


def _profiel_lookup(profiel_df: pd.DataFrame) -> dict:
    """Bouwt {'mm-dd HH:MM': (productie, consumptie)} uit een jaarprofiel."""
    if not isinstance(profiel_df.index, pd.DatetimeIndex):
        profiel_df = profiel_df.set_index(pd.to_datetime(profiel_df["timestamp"]))
    keys = profiel_df.index.strftime("%m-%d %H:%M")
    lookup = {}
    for key, prod, cons in zip(keys, profiel_df["productie"], profiel_df["consumptie"]):
        lookup[key] = (float(prod), float(cons))  # bij dubbele keys wint de laatste
    return lookup


def _leg_profiel_op_jaar(lookup: dict, prijzen: pd.Series) -> pd.DataFrame:
    """Zet het jaarprofiel op de kalender van een prijsreeks (kwartierraster)."""
    keys = prijzen.index.strftime("%m-%d %H:%M")
    prod = np.full(len(prijzen), np.nan)
    cons = np.full(len(prijzen), np.nan)
    for i, key in enumerate(keys):
        if key not in lookup and key.startswith("02-29"):
            key = "02-28" + key[5:]          # schrikkeldag: herhaal 28 februari
        if key in lookup:
            prod[i], cons[i] = lookup[key]

    # Kleine gaten (bv. zomertijduur die tussen profieljaar en prijsjaar op een
    # andere datum valt) opvullen met het laatst bekende kwartier. Grote gaten
    # duiden op een onvolledig profiel → fout.
    ontbrekend = int(np.isnan(cons).sum())
    if ontbrekend > 8:
        raise ValueError(
            f"Profiel mist {ontbrekend} kwartieren; volledig jaarprofiel vereist."
        )
    if ontbrekend:
        prod = pd.Series(prod).ffill().bfill().to_numpy()
        cons = pd.Series(cons).ffill().bfill().to_numpy()

    return pd.DataFrame(
        {"productie": prod, "consumptie": cons, "da_prijs": prijzen.to_numpy()},
        index=prijzen.index,
    )


def backtest_jaren(
    profiel_df: pd.DataFrame,
    battery: BatteryParams,
    tariff: TariffParams,
    jaren: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026),
) -> pd.DataFrame:
    """
    Rekent het profiel voor elk jaar door: eerst zonder batterij, dan met.

    profiel_df: één jaar kwartierdata met kolommen 'productie', 'consumptie'
                (kWh/kwartier) en een DatetimeIndex of 'timestamp'-kolom.

    Retourneert per jaar (rijen): kost_zonder_eur, kost_met_eur, besparing_eur,
    da_gemiddeld_eur_mwh, n_kwartieren.
    """
    lookup = _profiel_lookup(profiel_df)
    rijen = []
    for jaar in jaren:
        prijzen = haal_da_prijzen_jaar(jaar)
        df_jaar = _leg_profiel_op_jaar(lookup, prijzen)
        uit = vergelijk_zonder_met(df_jaar, battery, tariff)
        rij = {
            "jaar": jaar,
            "kost_zonder_eur": uit["zonder"]["totaal_eur"],
            "kost_met_eur": uit["met"]["totaal_eur"],
            "besparing_eur": uit["besparing_eur"],
            "da_gemiddeld_eur_mwh": float(prijzen.mean()),
            "n_kwartieren": len(df_jaar),
        }
        # Kostenuitsplitsing per kant, voor grafieken in het dashboard
        for kant in ("zonder", "met"):
            for comp in ("energie_eur", "netkost_var_eur", "capaciteit_eur",
                         "injectie_opbrengst_eur"):
                rij[f"{kant}_{comp}"] = uit[kant][comp]
        rijen.append(rij)
    return pd.DataFrame(rijen).set_index("jaar")
