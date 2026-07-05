"""
BESS-simulator API — dunne HTTP-laag bovenop de rekenmodules, bedoeld als
backend voor het (Lovable-)dashboard.

Starten:
    uvicorn bess_api:app --host 0.0.0.0 --port 8000

Interactieve documentatie (OpenAPI): http://localhost:8000/docs

Endpoints:
    GET  /health        — draait de API?
    GET  /netgebieden   — beschikbare Synergrid-netgebieden (Fluvius, ORES, ...)
    POST /valideer      — CSV met meterdata uploaden + valideren → profiel_id
    POST /simulatie     — backtest zonder/met batterij + terugverdientijd

Profielen van /valideer worden in het geheugen bewaard (single-user opzet):
na een herstart van de API moet de CSV opnieuw geüpload worden.
"""

from __future__ import annotations

import io
import os
import pathlib
import secrets
import uuid
from dataclasses import asdict
from typing import Literal, Union

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from bess_backtest import backtest_met_grafieken
from bess_core import (
    CRATE_DELERS,
    BatteryParams,
    irr_calc,
    npv_calc,
    reconstruct_profiles,
    tariff_simpel,
)
from bess_ingest import validate as ingest_validate
from bess_profielen import (
    DEFAULT_NETGEBIED,
    beschikbare_netgebieden,
    maak_standaard_profiel,
)

_KWARTIEREN_PER_JAAR = 35040

# ---------------------------------------------------------------------------
# API-key-beveiliging
#
# De key komt uit (in volgorde): env var BESS_API_KEY, of het bestand
# .bess_api_key in de projectroot. Bestaat geen van beide, dan wordt bij de
# eerste start een veilige key gegenereerd en naar .bess_api_key geschreven
# (staat in .gitignore). Alle endpoints behalve /health en /docs vereisen de
# header:  x-api-key: <key>
# ---------------------------------------------------------------------------

_API_KEY_FILE = pathlib.Path(__file__).parent / ".bess_api_key"


def _lees_of_maak_api_key() -> str:
    env = os.environ.get("BESS_API_KEY")
    if env:
        return env.strip()
    if _API_KEY_FILE.exists():
        return _API_KEY_FILE.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(32)
    _API_KEY_FILE.write_text(key, encoding="utf-8")
    print(f"Nieuwe API-key aangemaakt in {_API_KEY_FILE.name} — geef deze mee "
          "als 'x-api-key'-header in het dashboard.")
    return key


_API_KEY = _lees_of_maak_api_key()
_api_key_header = APIKeyHeader(name="x-api-key", auto_error=False)


def vereis_api_key(key: str | None = Depends(_api_key_header)) -> None:
    if key is None or not secrets.compare_digest(key, _API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Ongeldige of ontbrekende x-api-key-header.",
        )


app = FastAPI(
    title="BESS-simulator API",
    version="0.1",
    description="Batterijsimulatie met LP-optimalisatie tegen Belgische DA-prijzen.",
)

# Lovable-frontend draait op een ander domein → CORS open (single-user tool)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory profielopslag: {profiel_id: DataFrame met productie/consumptie}
_PROFIELEN: dict[str, pd.DataFrame] = {}


# ---------------------------------------------------------------------------
# Request-modellen
# ---------------------------------------------------------------------------

class StandaardProfiel(BaseModel):
    """Synergrid RLP0N-lastprofiel + optioneel PVGIS-zonnepanelen."""
    type: Literal["standaard"] = "standaard"
    jaarverbruik_kwh: float = Field(gt=0, description="Jaarverbruik in kWh")
    netgebied: str = DEFAULT_NETGEBIED
    kwp: float = Field(0.0, ge=0, description="PV-piekvermogen (0 = geen PV)")
    helling_graden: float = 35.0
    azimut_graden: float = Field(0.0, description="PVGIS: 0=zuid, -90=oost, +90=west")
    lat: float = 50.85
    lon: float = 4.35


class UploadProfiel(BaseModel):
    """Eerder via /valideer geüpload meterdataprofiel."""
    type: Literal["upload"] = "upload"
    profiel_id: str


class BatterijInput(BaseModel):
    capacity_kwh: float = Field(gt=0)
    crate: str = "1 op 2"
    dod: float = Field(0.8, gt=0, le=1)
    efficiency: float = Field(0.95, gt=0, le=1)
    soc_start: float = Field(0.5, ge=0, le=1)


class TariefInput(BaseModel):
    cap_eur_kw_jaar: float = Field(40.0, description="Capaciteitstarief €/kW/jaar")
    var_netkost_eur_kwh: float = Field(
        0.003, description="Variabele netkosten + taksen, €/kWh afname"
    )


class FinancieelInput(BaseModel):
    battery_price_eur_per_kwh: float = 685.0
    install_frac: float = 0.15
    maintenance_frac: float = 0.015
    lifetime_years: int = 16
    discount_rate: float = 0.06


class SimulatieRequest(BaseModel):
    profiel: Union[StandaardProfiel, UploadProfiel] = Field(discriminator="type")
    batterij: BatterijInput
    tarief: TariefInput = TariefInput()
    financieel: FinancieelInput = FinancieelInput()
    jaren: list[int] = [2022, 2023, 2024, 2025, 2026]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/netgebieden", dependencies=[Depends(vereis_api_key)])
def netgebieden() -> dict:
    return {"netgebieden": beschikbare_netgebieden()}


@app.post("/valideer", dependencies=[Depends(vereis_api_key)])
async def valideer(
    file: UploadFile = File(...),
    unit: str = Form("auto"),
    sep: str | None = Form(None),
    decimal: str = Form("."),
) -> dict:
    """
    CSV met kwartier-meterdata uploaden en valideren.

    Kolommen: timestamp, afname, injectie, optioneel pv_productie.
    sep=None laat pandas het scheidingsteken zelf detecteren (werkt ook
    voor Fluvius-exports met puntkomma's).

    Retourneert het validatierapport + een profiel_id voor /simulatie.
    """
    inhoud = await file.read()
    try:
        raw = pd.read_csv(
            io.BytesIO(inhoud),
            sep=sep if sep else None,
            engine="python",
            decimal=decimal,
        )
        raw.columns = raw.columns.str.strip()
        schoon, rapport = ingest_validate(raw, unit=unit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    profiel = reconstruct_profiles(schoon).set_index("timestamp")

    profiel_id = str(uuid.uuid4())
    _PROFIELEN[profiel_id] = profiel

    return {
        "profiel_id": profiel_id,
        "rapport": asdict(rapport),
        "jaarverbruik_kwh": float(profiel["consumptie"].sum()),
        "jaarproductie_kwh": float(profiel["productie"].sum()),
    }


@app.post("/simulatie", dependencies=[Depends(vereis_api_key)])
def simulatie(req: SimulatieRequest) -> dict:
    """
    Backtest: het profiel wordt per jaar tegen de werkelijke Belgische
    DA-prijzen doorgerekend — eerst zonder batterij, dan met (LP-optimaal).
    Het verschil is de besparing. Afgesloten met de terugverdientijd.
    """
    # --- Profiel opbouwen ---
    if isinstance(req.profiel, UploadProfiel):
        profiel = _PROFIELEN.get(req.profiel.profiel_id)
        if profiel is None:
            raise HTTPException(
                status_code=404,
                detail="Onbekend profiel_id — upload de CSV (opnieuw) via /valideer.",
            )
    else:
        try:
            profiel = maak_standaard_profiel(
                jaarverbruik_kwh=req.profiel.jaarverbruik_kwh,
                kwp=req.profiel.kwp,
                netgebied=req.profiel.netgebied,
                helling_graden=req.profiel.helling_graden,
                azimut_graden=req.profiel.azimut_graden,
                lat=req.profiel.lat,
                lon=req.profiel.lon,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # --- Parameters ---
    if req.batterij.crate not in CRATE_DELERS:
        raise HTTPException(
            status_code=400,
            detail=f"Ongeldige crate '{req.batterij.crate}'. Kies uit {list(CRATE_DELERS)}.",
        )
    batterij = BatteryParams(
        capacity_kwh=req.batterij.capacity_kwh,
        crate=req.batterij.crate,
        dod=req.batterij.dod,
        efficiency=req.batterij.efficiency,
        soc_start=req.batterij.soc_start,
    )
    tarief = tariff_simpel(
        cap_eur_kw_jaar=req.tarief.cap_eur_kw_jaar,
        var_netkost_eur_kwh=req.tarief.var_netkost_eur_kwh,
    )

    # --- Backtest (incl. grafiekdata per jaar) ---
    try:
        uit = backtest_met_grafieken(profiel, batterij, tarief, jaren=tuple(req.jaren))
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    tabel = uit["tabel"]

    # --- Terugverdientijd ---
    fin = req.financieel
    batterijkost = req.batterij.capacity_kwh * fin.battery_price_eur_per_kwh
    investering = batterijkost * (1 + fin.install_frac)
    onderhoud_jaar = batterijkost * fin.maintenance_frac

    # Geannualiseerd: deeljaren (YTD) wegen naar rato van hun kwartieren mee
    besparing_per_kwartier = tabel["besparing_eur"].sum() / tabel["n_kwartieren"].sum()
    besparing_jaar = besparing_per_kwartier * _KWARTIEREN_PER_JAAR
    netto_baten_jaar = besparing_jaar - onderhoud_jaar

    cashflows = [-investering] + [netto_baten_jaar] * fin.lifetime_years
    npv = npv_calc(fin.discount_rate, cashflows)
    irr = irr_calc(cashflows)
    terugverdientijd = investering / netto_baten_jaar if netto_baten_jaar > 0 else None

    return {
        "jaren": tabel.reset_index().to_dict(orient="records"),
        "grafieken": uit["grafieken"],
        "terugverdientijd": {
            "investering_eur": investering,
            "onderhoud_eur_jaar": onderhoud_jaar,
            "besparing_eur_jaar_geannualiseerd": besparing_jaar,
            "netto_baten_eur_jaar": netto_baten_jaar,
            "terugverdientijd_jaar": terugverdientijd,
            "npv_eur": npv,
            "irr": irr,
        },
        "beperkingen": [
            "Elke maand wordt onafhankelijk LP-geoptimaliseerd (perfecte prijskennis binnen de maand).",
            "Batterijdegradatie nog niet meegerekend.",
            "Deeljaren (YTD) worden geannualiseerd naar rato van het aantal kwartieren.",
        ],
    }
