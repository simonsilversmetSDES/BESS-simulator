---
name: release
description: Controleert en publiceert wijzigingen naar GitHub - volledige testsuite, geheimencontrole, commit en push. Gebruik na afgerond werk.
---

# Release-checklist: veilig committen en pushen

Doorloop deze stappen in volgorde. Stop en meld het als een stap faalt —
niet doorduwen naar de volgende stap.

## Stappen

1. **Volledige testsuite**: `pytest -q`. Alle tests moeten slagen (skips zijn
   OK — dat zijn cache-afhankelijke tests). Bij falen: eerst diagnosticeren en
   aan de gebruiker rapporteren; nooit testdrempels versoepelen om groen te
   forceren.

2. **Geheimencontrole**: controleer met `git status --short` en
   `git ls-files` dat het volgende NIET wordt meegecommit:
   - `.entsoe_key` (persoonlijke API-key)
   - `data/` (lokale caches)
   - `.venv/`, `__pycache__/`
   Staat er iets verdachts tussen de wijzigingen (een bestand dat je niet
   herkent, of iets wat op een credential lijkt): eerst de inhoud bekijken,
   dan pas beslissen.

3. **Commit**: duidelijke message in de stijl van de bestaande historie
   (Engels kopje, puntsgewijze toelichting mag Nederlands), afgesloten met de
   Co-Authored-By-trailer.

4. **Push**: `git push origin main`, en bevestig daarna met
   `git log origin/main..main --oneline | wc -l` dat er 0 lokale commits
   achterblijven.

5. **Rapporteer**: één regel per commit die gepusht is, plus het testresultaat.
