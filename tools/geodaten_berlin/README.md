# Freie Berliner Zusatzdaten für Ask the Map

`load_berlin_extras.py` lädt vier Themen und ordnet sie den Gebäuden in `emc.building` zu (ALKIS-ID `uuid`).

```
python load_berlin_extras.py                      # alles
python load_berlin_extras.py --nur rechenzentren  # einzeln: pv_potenzial, pv_bestand, eigentum, rechenzentren
```

| Thema | Quelle (frei) | Tabellen | Ebene |
|---|---|---|---|
| PV-Potenzial | Solarrechner Berlin, WFS `ua_solarpotenzial_solarrechner:d_photovoltaik_potenzial` (1,48 Mio. Dachflächen, `uid_gebaeude` = ALKIS-ID), DL-DE Zero 2.0 | `core_bldg.pv_potential_building` | Gebäude |
| PV-Bestand | Umweltatlas Solaranlagen, WFS `ua_solaranlagen_pv` (Basis Marktstammdatenregister) | `core_bldg.pv_installation`, `core_bldg.pv_installed_building`, `core_base.pv_small_plz` | Anlagen > 30 kWp und öffentliche Hand je Gebäude, kleine Anlagen nur je PLZ |
| Eigentum | Berliner Landesgrundvermögen, WFS `landesgrundvermoegen` (Bezirks-, Hauptverwaltungen, Liegenschaftsfonds/SILB/SODA) | `core_bldg.state_property_parcel`, `core_bldg.building_ownership` | Gebäude (≥ 50 % der Grundfläche auf landeseigenem Flurstück) |
| Eigentumsstruktur | Eigentumskonzentration Berlin 2025, WFS `eigentumstruktur` | `core_base.ownership_structure_pr` | nur Planungsraum |
| Rechenzentren | OpenStreetMap (Overpass, `telecom`/`building=data_center`, ODbL) und PeeringDB (Facilities Berlin) | `core_bldg.data_center`, `core_bldg.data_center_building` | Gebäude |

## Grenzen

* **Eigentum** betrifft das Grundstück, nicht das Gebäude (z. B. Lauben in Kleingärten auf Bezirksland, Erbbaurecht).
  Bund, Kirchen, landeseigene Gesellschaften (Wohnungsbaugesellschaften, Bäder-Betriebe), Genossenschaften und
  Private sind gebäudegenau nicht öffentlich; dafür gibt es nur die Anteile je Planungsraum.
* **PV-Bestand**: kleine private Anlagen (≤ 30 kWp) veröffentlicht das Marktstammdatenregister ohne Standort.
  Die beiden WFS-Ebenen überschneiden sich; Doppelungen (gleiche Adresse und Leistung) werden entfernt.
* **PV-Potenzial** ist eine Leistung in kWp, kein Ertrag in kWh/a und kein Zeitverlauf.
* **Rechenzentren**: nur kartierte (OSM) bzw. gemeldete (PeeringDB) Standorte, keine Vollständigkeit. Punkte werden
  dem Gebäude zugeordnet, das sie enthält, sonst dem nächsten innerhalb 50 m.
* **Neubau** ist keine eigene Quelle: Baujahr ab 2010 (KWW-Klasse „ab 2010“). Die Baujahre liegen nur als
  blockweise Baualtersklassen vor; die jüngste Klasse liegt um 2013, neuere Gebäude sind oft älter datiert.

## Auf den VPS

Lokal laden, dann die Tabellen per `pg_dump -t ...` übertragen (wie bei `tools/kww`), weil die VPS-DB dieselben
`emc.building`-IDs hat.
