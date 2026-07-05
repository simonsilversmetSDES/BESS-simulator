# BESS-simulator

Batterijsimulator voor Belgische businesscases: kwartier-meterdata (of een
Synergrid-standaardprofiel + PVGIS-zonnepanelen) wordt doorgerekend tegen echte
Belgische day-ahead-prijzen (2022–heden), eerst zonder en dan met batterij.
De batterijdispatch wordt per kalendermaand LP-geoptimaliseerd (PuLP/CBC),
inclusief capaciteitstarief op de maandpiek. Verschil in kost = besparing.
FastAPI-backend (`bess_api`) voedt een Lovable-dashboard (in aanbouw).

De eigenaar is een vibecoder zonder software-achtergrond: leg beslissingen uit
in mensentaal, vermijd jargon zonder toelichting.

## Architectuur (dataflow)

| Module | Rol |
|---|---|
| `bess_ingest.py` | CSV-meterdata inlezen + valideren (kW/kWh-detectie, gaten, resolutie) |
| `bess_profielen.py` | Synergrid RLP0N-standaardprofielen per netgebied, schaling naar jaarverbruik |
| `bess_pv.py` | PV-kwartierprofiel via PVGIS v5.3 (kWp, helling, azimut) |
| `bess_prices.py` | DA-prijzen België via ENTSO-E, cache-first in `data/` |
| `bess_core.py` | Rekenkern: greedy `simulate()`, LP `simulate_lp()`, `TariffParams`, `tariff_simpel()`, `vergelijk_zonder_met()`, `summarize_lp()` |
| `bess_backtest.py` | Jaarprofiel over meerdere prijsjaren leggen (schrikkeldag + DST afgehandeld) |
| `bess_api.py` | FastAPI-laag: `/health`, `/netgebieden`, `/valideer`, `/simulatie` |

## Vaste conventies — nooit stilzwijgend wijzigen

- **Efficiëntie is one-way** (`_EFF_CONVENTION` in bess_core): laden verliesloos,
  ontladen levert `d·η`; SOC daalt met `d` (gross). Excel-conform.
- **Eenheden**: energie in kWh/kwartier; DA-prijzen in €/MWh aan de buitenkant,
  €/kWh binnen het LP (deling door 1000 bij de LP-grens).
- **`netkost_injectie_eur_kwh`** wordt uitsluitend verrekend in
  `TariffParams.injectie_vergoeding_per_kwh()` — nergens anders aftrekken.
- **Besparing = werkelijke kostendelta** (kost zonder − kost met batterij).
  De baseline-injectieopbrengst is géén batterijbaat. Bewaakt door
  `test_lp.py::TestJaarbatenIsWerkelijkeKostendelta`.
- **LP-constraint `g_exp ≥ d_inj·η`** voorkomt dat injectie-ontlading zich als
  eigenverbruik vermomt — niet verwijderen.
- Nederlandse namen voor nieuwe publieke functies; Belgische notatie
  (€ 1.234,56 / dd/mm/jjjj) in gebruikersgerichte output.

## Nooit doen

- `.entsoe_key`, `.bess_api_key` of `data/` committen (staan in .gitignore; keys zijn geheim).
- `simulate()` (greedy), `bess_ingest.py`, `conftest.py`, `test_bess.py` of
  `test_ingest.py` wijzigen zonder expliciete vraag van de gebruiker.
- Testdrempels versoepelen of tests aanpassen om een falende test groen te
  krijgen — eerst de oorzaak diagnosticeren, dan pas beslissen of motor of
  test fout zit.

## Commando's

```bash
pytest -q                                      # volledige suite (~2 min met cache)
uvicorn bess_api:app --host 0.0.0.0 --port 8000   # API starten
# API-documentatie: http://localhost:8000/docs
```

## Datacaches (`data/`, wordt automatisch gevuld)

- DA-prijzen: eerste gebruik downloadt via ENTSO-E — vereist `.entsoe_key`
  (één regel met de API-key, in projectroot).
- Synergrid-profielen en PVGIS: key-loos, downloaden zichzelf.
- Tests die de cache nodig hebben slaan zichzelf over als die ontbreekt.

## Specs

- `BESS_Simulatie_Spec.md` — basisspecificatie (ingest, simulatie, financieel).
- `BESS_Spec_v8_LP_optimalisatie.md` — LP-formulering (§8), validatie-ankers §8.6–8.7.
