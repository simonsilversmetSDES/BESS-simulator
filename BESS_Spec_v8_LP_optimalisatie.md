# Specuitbreiding §8 — Kostenoptimaliserende dispatch (LP) met volledige gridkosten

> Voeg toe aan `BESS_Simulatie_Spec.md`. Vervangt de greedy DA-logica uit §3
> (kolommen R/W/T in de oude `simulate()`). De energiebalans-aggregaties uit §4
> blijven gelden; alleen de manier waarop laad/ontlaadbeslissingen tot stand
> komen verandert, plus de financiële afrekening (§4 financieel) wordt herzien.

## 8.0 Waarom deze herziening

De oude motor reageert per kwartier op de DA-prijs ("laad op de bodem"). Hij
optimaliseert nergens naartoe, rekent geen gridkosten mee, en maakt geen
onderscheid tussen laden-voor-eigenverbruik en laden-voor-injectie. Dat is
economisch fout: laden om te injecteren betaalt de volle afname-netkost zonder
recuperatie, terwijl laden voor eigenverbruik alleen netkost op de
batterijverliezen "verspilt".

Doel van de nieuwe motor: **minimaliseer de totale energiekost over de horizon**,
gegeven de volledige kostenstructuur, met de batterijfysica als constraints.

## 8.1 Kostencomponenten (nieuwe inputs)

Belgische factuur = energiekost + netkosten + toeslagen. Het capaciteitstarief
slaat **alleen op afname** (niet op injectiepieken van PV). Modelleer als:

```python
@dataclass
class TariffParams:
    # --- Volumetrisch, op AFNAME (per kWh) ---
    netkost_afname_eur_kwh: float        # distributienetkost afname
    toeslagen_afname_eur_kwh: float      # ODV, heffingen, accijns
    # energiekost afname = DA-prijs per kwartier (dynamisch), apart aangeleverd

    # --- Volumetrisch, op INJECTIE (per kWh) ---
    netkost_injectie_eur_kwh: float      # meestal ~0 voor < bepaalde drempel
    injectievergoeding_basis: str        # 'da' | 'vast'
    injectievergoeding_vast_eur_kwh: float | None   # indien 'vast'
    # bij 'da': vergoeding = DA-prijs per kwartier (evt. met afslag-factor)
    injectie_da_factor: float = 1.0      # bv. 0.9 als leverancier marge neemt

    # --- Capaciteitstarief, op MAANDPIEK afname (per kW) ---
    capaciteitstarief_eur_kw_maand: float
    cap_min_piek_kw: float = 2.5         # forfaitaire minimumpiek

    # --- Vaste kosten (informatief, raken optimalisatie niet) ---
    databeheer_eur_jaar: float = 0.0
```

> Bron-eenheden (verifieer met actuele Fluvius-tariefkaart van het netgebied):
> netkost afname in €/kWh, capaciteitstarief in €/kW/maand op de maandpiek
> (= hoogste kwartiervermogen in kW binnen de maand). Injectie krijgt in
> laagspanning < 10 kVA vaak geen netkost; voor jouw MW-schaalprojecten geldt
> wel een injectienetkost — vandaar de aparte parameter.

### De drie kostenscenario's die de motor moet onderscheiden

| Actie | Energiekost | Netkost afname | Capaciteits-impact |
|---|---|---|---|
| Direct eigenverbruik PV | 0 | vermeden | vermeden (verlaagt piek) |
| Laden uit PV → later eigenverbruik | 0 | vermeden (op verbruik) | kan piek verlagen |
| Laden uit net → later eigenverbruik | DA × geladen | betaald bij inkoop; vermeden bij verbruik → netto alleen op **verliezen** | laden verhoogt piek mogelijk |
| Laden uit net → injectie (markt-arbitrage) | DA × geladen | **volledig betaald**, niet gerecupereerd | laden verhoogt piek; injectie telt niet mee |

Dit is exact het onderscheid dat de gebruiker vroeg. In het LP volgt het vanzelf
uit de coëfficiënten in de doelfunctie.

## 8.2 LP-formulering

Per kwartier *t* (lengte Δ = 0,25 h) zijn de beslissingsvariabelen:

```
c_pv[t]   ≥ 0   # laden uit eigen PV-overschot (kWh)
c_grid[t] ≥ 0   # laden uit het net (kWh)
d_self[t] ≥ 0   # ontladen voor eigenverbruik (kWh)
d_inj[t]  ≥ 0   # ontladen om te injecteren / verkopen (kWh)
soc[t]    ≥ 0   # batterijniveau einde kwartier (kWh)
g_imp[t]  ≥ 0   # netafname dit kwartier (kWh)
g_exp[t]  ≥ 0   # netinjectie dit kwartier (kWh)
peak[m]   ≥ 0   # maandpiek afname (kW) voor maand m
```

Gegeven per kwartier: `prod[t]`, `cons[t]` (gereconstrueerd uit §2), `da[t]`.

### Doelfunctie (minimaliseren)

```
min  Σ_t [ g_imp[t] · (da[t] + netkost_afname + toeslagen_afname)        # kost afname
         − g_exp[t] · (injectievergoeding[t] − netkost_injectie) ]        # opbrengst injectie
   + Σ_m peak[m] · capaciteitstarief_eur_kw_maand                          # capaciteitskost
```

waarbij `injectievergoeding[t] = da[t]·injectie_da_factor` (basis 'da') of de
vaste waarde. Eenheden: alle volumetrische termen in €/kWh × kWh; DA in €/MWh
→ deel door 1000 of normaliseer vooraf.

### Constraints per kwartier

```
# Energiebalans van het aansluitpunt
g_imp[t] − g_exp[t] = cons[t] − prod[t] + c_grid[t] − d_self[t]·η − d_inj[t]·η + c_pv[t]
   # interpretatie: net = (vraag − eigen PV) + wat je laadt − wat de batterij levert

# Batterij-SOC met afgifte-efficiëntie η (one-way op ontladen, conform Excel)
soc[t] = soc[t−1] + c_pv[t] + c_grid[t] − (d_self[t] + d_inj[t]) / η_dis_factor
   # gebruik consistente efficiëntiedefinitie; zie §8.4

# SOC-grenzen (DOD-floor en capaciteit)
floor ≤ soc[t] ≤ capacity            # floor = capacity·(1−DOD)

# Vermogensgrenzen per kwartier (C-rate)
c_pv[t] + c_grid[t]  ≤ p_max · Δ
d_self[t] + d_inj[t] ≤ p_max · Δ

# PV-laden begrensd door beschikbaar overschot
c_pv[t] ≤ max(prod[t] − cons[t], 0)

# Eigenverbruik-ontlading begrensd door resterende vraag
d_self[t]·η ≤ max(cons[t] − prod[t], 0)

# Maandpiek koppelt alle kwartieren van maand m (capaciteitstarief)
peak[m] ≥ g_imp[t] / Δ   voor alle t in maand m        # kW = kWh / 0,25h
peak[m] ≥ cap_min_piek_kw
```

> De maandpiek-constraint is wat dag-per-dag-decompositie blokkeert: alle
> kwartieren van een maand delen dezelfde `peak[m]`. Zie §8.3 voor de keuze
> tussen exact en pragmatisch.

### Markt-arbitrage (laden→injecteren)
Geen aparte regel nodig. `c_grid[t]` + `d_inj[t]` samen vormen de arbitrage; de
solver kiest ze alleen als de spread `da[duur] − da[goedkoop]` groter is dan
`netkost_afname + verliezen`. Wil je arbitrage uitschakelen: forceer `d_inj[t]=0`.

## 8.3 Twee implementatievarianten

**Variant A — exact (aanbevolen voor correctheid).**
Eén LP per maand (alle kwartieren van die maand samen, ~2880 stappen), zodat de
maandpiek correct als variabele meegaat. 12 LP's per jaar. Solver: `PuLP` met
CBC (gratis), of `cvxpy`. Behapbaar: een maand-LP met ~2880×8 variabelen lost in
seconden.

**Variant B — pragmatisch tweetraps (sneller, benadering).**
1. Optimaliseer energiearbitrage per dag (96 stappen, 365 LP's) zónder
   capaciteitsterm.
2. Naverwerking: bepaal de maandpiek uit het resultaat en pas een
   piekafvlak-heuristiek toe (ontlaad extra rond de hoogste afname-kwartieren).
Sneller maar suboptimaal; de wisselwerking energie↔piek wordt niet geïntegreerd
opgelost.

> Default: **Variant A** per maand. Val terug op B alleen als A te traag blijkt
> op echte jaardata.

## 8.4 Efficiëntie-conventie (let op, bug-risico)

De oude code past η alleen toe op ontladen (one-way), wat de round-trip
onderschat. Beslis expliciet:
- **One-way (Excel-conform):** verlies alleen bij afgifte → `d·η` geleverd.
- **Round-trip gesplitst (correcter):** `√η` bij laden én `√η` bij ontladen.

Documenteer de keuze; de validatie-ankers (§7) zijn op one-way gebaseerd.

## 8.5 Output-uitbreiding (§4 herzien)

Naast de bestaande energiemetrieken, splits de financiële afrekening in
componenten zodat de UI ze los kan tonen:

```
besparing_energie_eur          # vermeden DA-kost door eigenverbruik + arbitrage
besparing_netkost_afname_eur   # vermeden volumetrische netkost
besparing_capaciteit_eur       # vermeden capaciteitstarief (piekafvlakking!)
besparing_toeslagen_eur        # vermeden heffingen
arbitrage_marge_eur            # netto markt-arbitrage na alle gridkosten
injectie_opbrengst_eur         # vergoeding voor injectie
```

De som hiervan − onderhoud = jaarbaten → NPV/IRR/breakeven (ongewijzigd t.o.v.
gefixte §4). Belangrijk: rapporteer `besparing_capaciteit_eur` apart — dat is
vaak de verrassend grote post voor C&I-klanten en het sterkste verkoopargument.

## 8.6 Bug die meeverhuist als je niet oplet

In de oude `summarize()` werd `da_kost` negatief bij negatieve DA-prijzen
(je wordt betaald om te laden). In het LP is dat *correct* gedrag, maar
controleer dat de gridkosten (`netkost_afname`) óók bij negatieve DA-prijzen
betaald worden — het net rekent zijn kost ongeacht de marktprijs. De totale
inkoopkost per kWh = `max(da, ...)` is FOUT; het is gewoon `da + netkost`, ook
als `da < 0`. Zet dit expliciet in een test.

## 8.7 Nieuwe validatie-ankers
Bouw deze sanity-checks:
- Met capaciteitstarief > 0 en een uitgesproken middagpiek: de batterij verlaagt
  de maandpiek meetbaar → `besparing_capaciteit_eur > 0`.
- Markt-arbitrage (`d_inj`) is 0 zodra `netkost_afname` hoog genoeg is dat geen
  enkele spread hem dekt.
- Bij `netkost_afname = 0`, `capaciteitstarief = 0` en `injectie = da`: het LP
  reproduceert ongeveer de greedy-uitkomst van de oude motor (regressie-brug).
- Totale jaarkost met batterij ≤ totale jaarkost zonder batterij (de optimalisatie
  kan nooit slechter zijn dan niets doen).

## 8.8 Afhankelijkheid
Voeg toe aan requirements: `pulp>=2.8` (CBC-solver zit ingebouwd) of
`cvxpy>=1.4`. PuLP is lichter en voldoende voor een lineair probleem als dit.
