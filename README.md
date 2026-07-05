# BESS-simulator

Rekent uit wat een thuis- of bedrijfsbatterij in België **werkelijk** bespaart.
Een verbruiksprofiel (eigen meterdata of een officieel Synergrid-standaardprofiel,
eventueel aangevuld met zonnepanelen via PVGIS) wordt doorgerekend tegen de échte
Belgische day-ahead-prijzen van 2022 tot vandaag — eerst zonder batterij, dan met
een batterij die per maand wiskundig optimaal wordt aangestuurd (arbitrage,
eigenverbruik én peakshaving tegelijk). Het verschil in totale energiekost is de
besparing; daaruit volgt de terugverdientijd.

Voorbeeld (600 kWh-batterij, demoprofiel, echte prijzen):

| Jaar | Kost zonder | Kost met | Besparing |
|---|---|---|---|
| 2022 | € 155.391 | € 120.024 | € 35.367 |
| 2023 | € 82.122 | € 66.402 | € 15.719 |
| 2024 | € 73.569 | € 57.693 | € 15.875 |
| 2025 | € 86.653 | € 66.714 | € 19.939 |
| 2026 YTD | € 43.994 | € 30.454 | € 13.540 |

## Installatie (nieuwe pc)

Vereist: Python 3.11+ en Git.

```bash
git clone https://github.com/simonsilversmetSDES/BESS-simulator.git
cd BESS-simulator
python -m venv .venv
.venv\Scripts\activate          # Windows; op Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

Maak daarna eenmalig het bestand `.entsoe_key` aan in de projectroot met je
persoonlijke ENTSO-E API-key (één regel). Deze key staat bewust niet op GitHub.

Controle dat alles werkt:

```bash
pytest -q
```

Bij het eerste gebruik downloadt de tool zelf de prijzen (ENTSO-E), de
Synergrid-profielen en PVGIS-data naar `data/` — dat duurt eenmalig enkele minuten.

## API starten

```bash
uvicorn bess_api:app --host 0.0.0.0 --port 8000
```

Interactieve documentatie: <http://localhost:8000/docs>

Bij de eerste start wordt automatisch een API-key aangemaakt in `.bess_api_key`
(blijft lokaal, staat in .gitignore). Alle endpoints behalve `/health` vereisen
die key als header: `x-api-key: <inhoud van .bess_api_key>`.

| Endpoint | Key nodig | Doet |
|---|---|---|
| `GET /health` | nee | Draait de server? |
| `GET /netgebieden` | ja | Beschikbare netgebieden (Fluvius, ORES, RESA, Sibelga) |
| `POST /valideer` | ja | CSV-meterdata uploaden + valideren → `profiel_id` |
| `POST /simulatie` | ja | Backtest zonder/met batterij + terugverdientijd |

Voorbeeldrequest:

```json
POST /simulatie
{
  "profiel": {"type": "standaard", "jaarverbruik_kwh": 50000,
               "netgebied": "Fluvius Antwerpen", "kwp": 30},
  "batterij": {"capacity_kwh": 50, "crate": "1 op 2"},
  "tarief": {"cap_eur_kw_jaar": 40.0, "var_netkost_eur_kwh": 0.003},
  "jaren": [2024, 2025]
}
```

## Status en volgende stappen

Af: ingest + validatie · standaardprofielen · PVGIS · LP-optimalisatie ·
echte DA-prijzen 2022–2026 · backtest · kostenvergelijking · API · 98 tests.

| # | Stap | Omvang | Waarom |
|---|---|---|---|
| 1 | ~~Documentatielaag (dit bestand, CLAUDE.md, skills)~~ | klein | ✅ |
| 2 | ~~API-beveiliging (`x-api-key`-header)~~ | klein | ✅ |
| 3 | Cloudflare Tunnel op de mini-pc | klein | Publieke HTTPS-URL zonder poorten open te zetten |
| 4 | Lovable-dashboard tegen `openapi.json` | middel | De gebruikersinterface |
| 5 | Test met echte Fluvius-export | klein | Valideert de ingest op het echte bestandsformaat |
| 6 | Degradatie + RTE-splitsing in het financieel model | middel | Realistischer terugverdientijd (jaar 10 levert minder dan jaar 1) |
| 7 | Arbitrage-label in `summarize_lp` herzien; git-historie opschonen | klein | Netheid, geen blocker |

## Projectstructuur

```
bess_ingest.py      CSV-meterdata inlezen + valideren
bess_profielen.py   Synergrid RLP0N-standaardprofielen
bess_pv.py          PV-profiel via PVGIS
bess_prices.py      DA-prijzen via ENTSO-E (cache in data/)
bess_core.py        Rekenkern: LP-dispatch, tarieven, vergelijking
bess_backtest.py    Profiel doorrekenen over meerdere prijsjaren
bess_api.py         FastAPI-laag (dashboard-backend)
test_*.py           98 pytest-tests
```
