---
name: backtest
description: Draait een BESS-backtestscenario tegen echte Belgische DA-prijzen en toont de besparingstabel. Argumenten: jaarverbruik_kwh [kwp] [batterij_kwh] [jaren]. Voorbeeld = /backtest 50000 30 50
---

# Backtest-scenario draaien

Draai een backtest met de opgegeven parameters en presenteer het resultaat
als nette tabel in Belgische notatie.

## Argumenten (in volgorde, spaties ertussen)

1. `jaarverbruik_kwh` (verplicht) — bv. 50000
2. `kwp` (optioneel, default 0) — PV-piekvermogen
3. `batterij_kwh` (optioneel, default 50) — batterijcapaciteit
4. `jaren` (optioneel, default 2024 2025) — welke prijsjaren

Ontbrekende argumenten: gebruik de defaults, vraag niet door.

## Stappen

1. Draai dit script (pas de parameters aan; werkdirectory = projectroot):

```python
from bess_profielen import maak_standaard_profiel
from bess_core import BatteryParams, tariff_simpel
from bess_backtest import backtest_jaren

profiel = maak_standaard_profiel(jaarverbruik_kwh=<JAARVERBRUIK>, kwp=<KWP>)
bat = BatteryParams(capacity_kwh=<BATTERIJ_KWH>, crate="1 op 2",
                    dod=0.8, efficiency=0.95)
tabel = backtest_jaren(profiel, bat, tariff_simpel(), jaren=<JAREN>)
print(tabel.to_string(float_format=lambda x: f"{x:,.0f}"))
```

2. Presenteer per jaar: kost zonder, kost met, besparing, gemiddelde DA-prijs —
   als markdown-tabel met € en Belgische duizendtallen (€ 1.234).
3. Sluit af met één zin duiding: welk jaar springt eruit en waarom
   (prijsniveau of volatiliteit).

## Aandachtspunten

- Volledige backtest over 5 jaar duurt enkele minuten (LP per maand); meld dat
  vooraf als de gebruiker alle jaren vraagt.
- 2026 is een deeljaar (YTD) — zeg dat erbij als het in de tabel staat.
- Eerste run op een verse machine downloadt eerst prijzen/profielen naar `data/`.
