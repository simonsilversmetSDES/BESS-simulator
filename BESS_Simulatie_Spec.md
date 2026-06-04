# Functionele specificatie — BESS Autoconsumptie & DA-arbitrage simulator

**Bron:** Autoconsumptie_berekeningsfile.xlsx (tab `In PU` = rekenmotor, `Businessmodel battery` = financieel model)
**Doel:** rekenkern + UI die, op basis van geüploade kwartier-meterdata en gekozen batterijparameters, simuleert (1) hoeveel extra autoconsumptie de batterij oplevert en (2) hoeveel een batterij verdient door slim te laden op dynamische (day-ahead) tarieven.
**Architectuur:** Python-rekenkern (backend) → API → React/Lovable UI → Supabase (data + scenario's).

---

## 1. Scope (vs. de Excel)

In scope (deze tool):
- Upload van **netto injectie + netto afname per kwartier** (zoals digitale meter).
- Selectie van **batterijparameters** in de UI.
- Simulatie van **autoconsumptie met/zonder batterij** en **DA-arbitrage**.
- Output: energiebalansen + financiële businesscase (besparing, NPV, IRR, breakeven).

Bewust **buiten** scope (de Excel doet dit nog wel, maar jij snijdt het weg):
- PV-profielgeneratie uit SLP's en oriëntatie/azimuth.
- EV-laadprofielgeneratie.
- Verbruiksprofielen uit standaard load profiles (S11/S12/S21/S22).

Reden: de gebruiker levert al gemeten kwartierdata, dus de motor hoeft niets te *genereren*, alleen te *simuleren*.

---

## 2. Inputcontract

### 2.1 Data-upload (verplicht)
Tijdreeks, één rij per kwartier, idealiter 1 volledig jaar (35.040 of 35.136 rijen bij schrikkeljaar). Verwachte kolommen:

| Veld | Eenheid | Verplicht | Toelichting |
|---|---|---|---|
| `timestamp` | datetime (lokaal, kwartier) | ja | oplopend, gelijkmatige 15-min stappen |
| `afname` | kW of kWh/kwartier | ja | netto afname van het net (digitale meter) |
| `injectie` | kW of kWh/kwartier | ja | netto injectie op het net (digitale meter) |
| `pv_productie` | kW of kWh/kwartier | optioneel | indien bekend; anders gereconstrueerd |

**Eenheid-conventie:** de Excel rekent intern in energie per kwartier. Bij kW-input geldt `kWh_kwartier = kW × 0,25`. Leg dit vast in de validatie en converteer bij ingest.

### 2.2 Reconstructie productie & consumptie
De motor heeft per kwartier *bruto* productie en consumptie nodig. Uit netto meterdata:

```
# Net-saldo per kwartier
if pv_productie bekend:
    productie  = pv_productie
    consumptie = afname + productie - injectie      # Excel: 'Invoegen data'!E = C + B - D
else:
    # Geen PV-meting: alle injectie = onverbruikte productie, afname = resterende consumptie
    productie  = injectie
    consumptie = afname
```

> Validatie: `consumptie ≥ 0` en `productie ≥ 0` per kwartier. Negatieve waarden duiden op meet-/tekenfout in de upload → waarschuwing tonen.

### 2.3 Batterijparameters (UI-velden)
Uit Excel-tab `Parameters` + `In PU`!AD:

| Parameter | Symbool Excel | Voorbeeld | Eenheid | Default |
|---|---|---|---|---|
| Capaciteit | `Capaciteit` / AD8 | 600 | kWh | — |
| C-rate | AD11 | "1 op 2" | — | 1 op 2 |
| SOC start | Parameters | 0,5 | fractie | 0,5 |
| DOD (ontlaaddiepte) | `Ontlaaddiepte` | 0,8 | fractie | 0,8 |
| Round-trip efficiëntie | `Efficiency` | 0,95 | fractie | 0,95 |
| Slim laden op DA aan/uit | Parameters C25 ("ToU ifv DA") | ja | bool | ja |
| DA-laadfactor / drempel | Parameters factor 0,7 | 0,7 | — | 0,7 |

Afgeleiden:
- **Laadvermogen per kwartier** `Laadvermogen = Capaciteit × DOD / crate_deler` waarbij crate_deler ∈ {1,2,4,8} uit "1 op N". Per kwartier nog × 0,25h. (Excel AD12 = `Capaciteit × 0,8 / 2 = 240` voor 600 kWh @ 1-op-2; toegepast als `Laadvermogen × 0,25` in de dispatch.)
- **Bruikbare ruimte** wordt begrensd door `Capaciteit` (boven) en `DOD` (onder).

### 2.4 Financiële parameters
Uit `Businessmodel battery`:

| Parameter | Waarde | Eenheid |
|---|---|---|
| Prijs batterij | 685 | €/kWh |
| Onderhoud | 1,5% van batterijkost / jaar | — |
| Installatie + EMS | 15% van batterijkost (eenmalig) | — |
| Looptijd | 16 | jaar (Y0–Y16) |
| Discontovoet | (uit NPV-formule halen — zie §6 bug) | % |
| DA-prijsreeks | DA 2023 incl. T&D | €/MWh |
| T&D-opslag op afname | ×1,3 (kostkant) en ×0,2 → ×1,2 (incl. T&D-kolom G) | — |

---

## 3. De simulatiemotor (kwartier-voor-kwartier)

Dit is de letterlijke vertaling van tab `In PU`, kolommen H t/m AA. State (batterijniveau) draagt over van het vorige kwartier — het is een sequentiële loop, geen vectoroperatie.

Notatie per kwartier *t*:

```
H = productie − consumptie                      # overschot (+) of tekort (−)

# --- Laden uit PV (kolom T) ---
laadlimiet = Laadvermogen × 0.25
if H >= 0:                                       # overschot
    if H < laadlimiet:
        T = min(H, Capaciteit − niveau_vorig)
    else:
        T = min(laadlimiet, Capaciteit − niveau_vorig)
else:
    T = 0

# --- Slim laden uit net op DA (kolom W) ---
# Laad uit net als: DA aan staat, dit kwartier de laagste prijs is binnen het venster,
# verwachte PV de komende ~24u te laag is, en er niet al PV-geladen wordt.
if DA_aan and (productie_volgende_dag < drempel) and (DA_nu == min(DA_venster)) and T == 0:
    W = min(laadlimiet, Capaciteit − niveau_vorig)
else:
    W = 0
S = T + W                                         # totaal geladen

# --- Ontladen voor eigenverbruik (kolom R) ---
beschikbaar = niveau_vorig − (Capaciteit × (1 − DOD))   # = P-kolom
if H < 0 and W == 0:                             # tekort en niet aan het netladen
    behoefte = abs(H)
    if behoefte > laadlimiet:
        R = −min(laadlimiet, max(beschikbaar, 0))
    else:
        R = −min(behoefte, max(beschikbaar, 0))
else:
    R = 0

# --- Niveau-update met verliezen (kolommen Q, Y, Z) ---
niveau_ruw = niveau_vorig + R + S
verliezen  = abs(R) − abs(Efficiency × R)        # verlies op ontladen
niveau_t   = niveau_ruw − verliezen              # = Z, startpunt volgend kwartier

# --- Energiebalansen ---
autoconsumptie_zonder = productie if H < 0 else consumptie   # I-kolom (min(prod,cons))
autoconsumptie_met    = autoconsumptie_zonder + T × Efficiency  # J
injectie_zonder = max(H, 0)                                   # L
injectie_met    = injectie_zonder − (T×Eff + W×Eff)          # M
afname_zonder   = consumptie − productie + injectie_zonder    # N
afname_met      = consumptie − productie + injectie_met       # O
```

**Belangrijke nuances uit de Excel:**
- `Efficiency` (0,95) wordt toegepast bij *ontladen/levering*, niet symmetrisch. Controleer of je round-trip of one-way wilt; de Excel doet one-way op de afgegeven kant.
- De DA-laadbeslissing kijkt naar een **prijsvenster** (`MIN(D4:D27)` = laagste van komende 24 kwartieren in de Excel — verifieer vensterlengte; mogelijk bedoeld als dagvenster van 96 kwartieren) en naar **verwachte PV** (`SUM(C28:C123)` = som productie komende ~24u).

---

## 4. Outputmetrieken

Aggregaties over het jaar (Excel sommeert kolommen, deelt door 1000 voor MWh):

**Energie:**
- Autoconsumptie zonder vs. met batterij (MWh) → **extra autoconsumptie = Δ**
- Injectie zonder vs. met (MWh)
- Afname zonder vs. met (MWh)
- Geladen uit PV (T), geladen uit net op DA (W), ontladen (R) — alle in MWh

**Financieel (situatie zonder vs. met batterij):**
- Energiefactuur €/jaar (zonder vs. met)
- **Besparing autoconsumptie** = waarde van Δ-afname × tarief
- **DA-arbitrage** = verkoop ontladen energie × DA-prijs − kost geladen energie × (DA × 1,3 T&D)
- Investering = `Capaciteit × 685 × 1,15` (incl. installatie), onderhoud = `1,5% × Capaciteit × 685` per jaar
- **NPV, IRR, breakeven (jaren), ROI** over 16 jaar

**Visualisaties (UI):**
- Heatmaps batterijniveau per maand/week (Excel-tabs Heatmaps_*)
- Stacked revenue: besparing + DA-verkoop − kost − onderhoud, in k€/jaar
- Cumulatieve cashflow met breakeven-markering
- SOC-curve over een representatieve week

---

## 5. Bekende bugs in de Excel (NIET meeverhuizen)

1. **IRR = `#NUM!`** — de cashflowreeks is over de hele looptijd negatief (revenue-regel staat negatief), dus IRR convergeert niet. Oorzaak: `Revenu electricity` (regel in businessplan) is negatief ingevoerd terwijl het een opbrengst hoort te zijn → tekenfout. Fix de tekenconventie: opbrengsten positief, kosten negatief, en valideer dat jaar-1 cashflow positief kan zijn.
2. **Kapotte named ranges** — `Capaciteit`, `Laadstand0`, `Laadvermogen`, `Ontlaaddiepte`, `Verliezen` tonen `#REF!`. Reconstrueer ze uit de formules (gedaan in §2.3/§3); definieer ze schoon in de rekenkern als expliciete variabelen.
3. **DOD-toepassing** — controleer of `beschikbaar` correct de bodem (1−DOD)×capaciteit respecteert; in de Excel zit dit in de P-kolom via `Laadstand0 − Ontlaaddiepte` wat eenheden-inconsistent oogt. Zet om naar absolute kWh-grenzen.
4. **Vensterlengtes DA** — verifieer of het prijsvenster (24 vs 96 kwartieren) en het PV-vooruitkijkvenster bewust gekozen zijn; documenteer als expliciete parameter.

---

## 6. Bouwvolgorde (kern eerst, UI laatst)

1. **Rekenkern (Python).** Pure functie `simulate(meterdata_df, battery_params, da_prices, financial_params) → results`. Geen DB/HTTP. Unit-test elke fase tegen Excel-uitkomsten van het huidige scenario (600 kWh, 1-op-2, autoconsumptie 31,7%→…).
2. **Validatielaag.** Upload-parser: kolomdetectie, eenheid-conversie (kW↔kWh), tijdresolutie-check, gap-detectie, teken-/negatiefcheck.
3. **DA-prijsdata.** Referentiereeks in Supabase (DA 2023 incl. T&D, of live koppeling later). Per-kwartier gemapt op de upload-timestamps.
4. **API.** Endpoints: `POST /simulate`, `POST /upload` (+validatierapport), `GET/POST /scenario`.
5. **UI (Lovable).** Upload-scherm → batterij-configurator (sliders/dropdowns uit §2.3) → resultatendashboard (§4). UI roept API; rekent niets zelf.
6. **Validatie & dataclassificatie.** Volledige keten vs. Excel. Klant-verbruiksdata van geïdentificeerde partij = waarschijnlijk L3/L4 → bepaalt opslaglocatie en upload-flow.

---

## 7. Validatie-ankers (huidige Excel-scenario)
Gebruik deze als regressietest voor de kern:

| Metriek | Excel-waarde |
|---|---|
| Capaciteit | 600 kWh |
| C-rate | 1 op 2 → laadvermogen 240 kW |
| Efficiëntie | 0,95 |
| Autoconsumptie zonder batterij | ~439 MWh |
| Autoconsumptie met batterij | ~558 MWh |
| Energiefactuur zonder | €40.053/jaar |
| Energiefactuur met | €39.990/jaar |
| Investering | €411.000 + €61.650 installatie |
| NPV (16j) | −€550.317 (met huidige bug) |

> Na bugfix (§5) zullen NPV/IRR afwijken — dat is gewenst. Leg vóór de fix vast wat de *correcte* tekenconventie hoort te zijn, en herijk de ankers.
