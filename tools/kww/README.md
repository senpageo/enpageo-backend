# KWW-Technikkatalog → core_bldg (Heizwärme/Warmwasser, Prozesswärme-Relevanz)

Quelle: `KWW-Technikkatalog-Waermeplanung_08-2026_Version_1.1-1.xlsx`, Tab 53–56.

## Reihenfolge (lokal getestet am 16.11.2026, VPS noch nicht)

```
psql ... -f 01_schema_kww.sql                 # legt core_bldg.kww_* neu an
python 02_import_kww.py <KWW.xlsx>             # Tab 53–56 -> Tabellen, Gegenprüfung mit flatdata_all
psql ... -f 03_gfk_kww_mapping.sql             # ALKIS-GFK -> KWW-Typ + Tab-56-Funktion (mit Prüfungen)
python kww_waermebedarf.py --tabelle-schreiben # optional: Ergebnis für alle Gebäude x 4 Szenarien
```

Python: `openpyxl` (nur Import) und `psycopg` v3. Ohne `--dsn` wird `DATABASE_URL` aus `backend-cesium/.env` gelesen.

## Tabellen in core_bldg

| Tabelle | Inhalt |
|---|---|
| `kww_bldg_type` | 12 Typen (10 NWG Tab 53, EZFH Tab 54, MFH Tab 55), Faktor BGF/NGF |
| `kww_spec_demand` | kWh/(m² BGF·a) je Typ × Baualtersklasse × Szenario (256 Werte, Schwimmbad ab 2010 = k. A.) |
| `kww_scenario` | 2025, 2045_o45, 2045_uba, 2045_fortschreibung |
| `kww_reduction` | jährliche Reduktion und Niveau 2045 je Typ und Szenario |
| `kww_process_heat` | Tab 56: Prozesswärme Ja/Nein, Datenerfassung, Voraussetzung (41 Funktionen) |
| `kww_gfk_mapping` | 188 Berliner GFK → KWW-Typ, beheizt, Qualität (direkt/naeherung/kein_typ/unbeheizt), Tab-56-Funktion |
| `kww_note` | Anmerkungen HA–HS und Quellen |
| `kww_heat_demand_building` | Ergebnis je Gebäude und Szenario (aus `kww_waermebedarf.py`) |

## Rechenweg

`Bedarf = Grundfläche × oberirdische Geschosse × Kennwert(Typ, Baualtersklasse, Szenario)`

* Die KWW-Kennwerte sind **Endenergie Heizung + Warmwasser je BGF**, ohne Prozesswärme. Der Faktor BGF/NGF ist
  im Katalog schon eingerechnet und dient hier nur zur Ausgabe der NGF.
* Wohngebäude (GFK 1000/1010): EZFH/MFH aus EnergyMap-Typ (EFH/RH bzw. MFH/GMH/HH), sonst nach Geschosszahl.
* Baujahr aus `emp.emblive` (blockweise Klassenmitte); ohne Baujahr oder bei `k. A.`: Wert „Alle Klassen“.
* Geschosse: `aog`, sonst SimStadt-Geschosszahl, sonst Höhe/3 m, sonst 1 (Spalte `storeys_source`).
* Zwischenjahre: geometrisch zwischen 2025 und 2045.
* Prozesswärme: Tab 56 enthält **keine kWh-Werte**, nur die Relevanz. Entscheidung 17.11.2026: vorerst nur
  die Relevanz ausgeben (Spalten `process_heat`, `process_heat_data_collection`, `process_heat_precondition`).

## Offene Entscheidungen

* Reduktionsraten: Für einige NWG-Typen stehen in `flatdata_all` andere Werte (z. B. Industrie UBA 1,8 statt 1,4 %/a)
  als im Tabellenblatt. Übernommen sind die Tabellenblätter, weil deren 2045-Werte dazu passen.
* GFK 2142/2143 (Speicher, Lagerhalle) sind wie in der EnergyMap unbeheizt; KWW-Typ „Lagerung“ ist hinterlegt.
* Gewächshäuser, Technikgebäude und Krematorium haben keinen KWW-Typ (Bedarf leer statt geraten).
