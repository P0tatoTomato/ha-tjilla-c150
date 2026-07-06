# Tjilla C150 — Home Assistant integratie

Een volledig **lokale** Home Assistant-integratie voor de Tjilla C150 robotstofzuiger.
Geen cloud, geen account bij de fabrikant nodig voor de dagelijkse besturing — de
integratie praat rechtstreeks met de robot over je eigen netwerk via het
Tuya 3.3 lokale protocol.

> **Status:** v1.0.0. Werkend en in dagelijks gebruik, maar reverse-engineered
> op één apparaat. Zie [Beperkingen](#beperkingen).

## Waarom lokaal

De Tjilla C150 is een Tuya-gebaseerd apparaat. De officiële app stuurt commando's
via de Tuya-cloud, wat betekent: afhankelijkheid van een externe dienst, latency,
en je poetsgedrag dat een datacenter passeert. Deze integratie doet alles lokaal —
starten, stoppen, kamerreiniging, zuigkracht, status — zonder dat er een pakket je
huis verlaat. Je hebt de cloud eenmalig nodig om de `local_key` van je robot op te
halen; daarna niet meer.

## Functies

- **Volledige besturing**: starten, pauzeren, stoppen (ter plekke, zonder naar de
  dock te gaan) en terugsturen naar de dock — met knoppen die zich per robottoestand
  logisch gedragen.
- **Kamerreiniging**: selecteer één of meer kamers en stuur ze als één commando naar
  de robot. Losse "reinig deze kamer"-knoppen per kamer voor één-tik-bediening.
- **Directe UI-respons**: commando's tonen hun effect optimistisch; de echte status
  volgt via lokale statuspushes van de robot (`local_push`).
- **Sensoren**: batterij, reinigingstijd en -oppervlak, totalen, en de resterende
  levensduur van zijborstel, rolborstel, filter en dweildoek.
- **Instellingen**: zuigkracht, dweilintensiteit, reinigingstype, volume, niet-storen
  en meer.
- **Talen**: Nederlands en Engels.
- **Diagnostics**: downloadbare debug-dump met geredacteerde sleutels.

## Vereisten

- Home Assistant 2025.6 of nieuwer
- De robot en Home Assistant op hetzelfde lokale netwerk
- De `device_id` en `local_key` van je robot (zie [Installatie](#installatie))

## Installatie

### Via HACS (aanbevolen)

1. Zorg dat [HACS](https://hacs.xyz) is geïnstalleerd.
2. Voeg deze repository toe als *custom repository* (HACS → drie puntjes →
   *Custom repositories*), categorie **Integration**:
   ```
   https://github.com/P0tatoTomato/ha-tjilla-c150
   ```
3. Installeer "Tjilla C150" en herstart Home Assistant.

### Handmatig

Kopieer de map `custom_components/tjilla_c150` naar je Home Assistant
`config/custom_components/`-map en herstart.

### De local_key ophalen

Deze integratie is lokaal, maar de robot geeft zijn sleutel niet zomaar prijs.
Je haalt hem eenmalig op met de open-source
[**tinytuya**](https://github.com/jasonacox/tinytuya)-wizard:

```bash
pip install tinytuya
python -m tinytuya wizard
```

De wizard begeleidt je door het (gratis) aanmaken van een Tuya IoT-project en
geeft je per apparaat de `device_id`, het lokale IP-adres en de `local_key`.
Deze drie vul je in bij het toevoegen van de integratie in Home Assistant.

> Sluit tijdens gebruik de officiële Smart Life / Tuya-app volledig af: een
> Tuya-apparaat accepteert maar één lokale verbinding tegelijk.

## Configuratie

Voeg de integratie toe via **Instellingen → Apparaten & Services → Integratie
toevoegen → Tjilla C150**. Vul IP-adres, `device_id` en `local_key` in. Daarna
kun je in de opties je kamers benoemen (het kamer-ID zoals de robot het kent,
plus een naam naar keuze).

### Kamer-ID's vinden

De robot nummert kamers intern. Welk nummer bij welke kamer hoort, ontdek je door
te testen: selecteer een kamer-ID, start de reiniging, en kijk waar de robot heen
rijdt. De ID's zijn doorgaans 1-based.

## Beperkingen

Dit is reverse-engineering-werk op één apparaat (firmware 1.5.32). Wat werkt, is
op de Tjilla C150 getest maar:

- **Geen kaartweergave.** Het lokale protocol levert wel kaartdata, maar het
  decoderen daarvan valt buiten de scope van deze integratie. Kamerreiniging werkt
  via de automatische smart functie of kamer-ID's, niet via een visuele kaart.
- **Kamernamen configureer je handmatig.** De robot pusht ze niet lokaal.
- **Getest op de Tjilla C150.** Andere Tuya-robots met dezelfde DP-structuur zouden
  kunnen werken, maar zijn niet getest.

## Vibecoding-disclaimer

Deze integratie is grotendeels tot stand gekomen via **vibecoding**.

De code werkt en wordt actief gebruikt maar is niet
door een team van ervaren HA-ontwikkelaars gereviewd. Gebruik het met dat in het
achterhoofd, en meld gerust wat je tegenkomt via de
[issues](https://github.com/P0tatoTomato/ha-tjilla-c150/issues).

## Credits

Deze integratie staat op de schouders van open-source werk:

- **[tinytuya](https://github.com/jasonacox/tinytuya)** (Jason Cox) — de bibliotheek
  die alle lokale Tuya 3.3-communicatie mogelijk maakt, en de wizard waarmee je je
  `local_key` ophaalt. Het fundament onder deze integratie.
- **[robottino-rs](https://github.com/bennesp/robottino-rs)** (bennesp) — een
  onafhankelijke reverse-engineering van een Tuya-robotstofzuiger met dezelfde
  DP-structuur. Met name voor het lokale kamerreiniging-commando.
- **[tuya-sign-hacking](https://github.com/nalajcie/tuya-sign-hacking)** (nalajcie)
  en **[tuya_cloud_map_extractor](https://github.com/oven-lab/tuya_cloud_map_extractor)**
  (oven-lab) — waardevol referentiemateriaal bij het begrijpen van het Tuya-protocol
  en de kaartdata.
- **[Home Assistant](https://www.home-assistant.io)** en het bredere lokale-smarthome-
  ecosysteem — waar deze integratie op draait en op is geïnspireerd.

En met dank aan de reverse-engineering-gemeenschap rond Tuya-apparaten in het algemeen.

## Licentie

Uitgebracht onder de [MIT-licentie](LICENSE).

Deze integratie is niet gelieerd aan, goedgekeurd door of ondersteund door Tjilla,
Tuya of enige fabrikant. Alle merknamen zijn eigendom van hun respectieve eigenaren.
Gebruik op eigen risico.
