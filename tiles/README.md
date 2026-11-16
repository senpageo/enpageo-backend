# 3D-Tiles-Pipeline für die Cesium-Karte

Baut aus der EnergyMap-Datenbank (`emc`, `emp`, `citydb` = 3DCityDB v4) statische 3D Tiles 1.1 für die
Cesium-Karte (Website-Route `/[locale]/maps-cesium`). Die Datenbank wird nur gelesen.

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python export_region.py --region Neukölln --out data/neukoelln   # DB -> CSV je 1-km-Zelle
.venv/Scripts/python build_region.py data/neukoelln out/neukoelln              # CSV -> Tileset
```

Für ganz Berlin (Stand 16.11.2026: Export 7 min mit `--jobs 6` gegen die lokale DB, Bau 9 min, 1,3 GB):

```bash
export PSQL_CMD="docker exec -i enpageo-db psql -U enpageo -d enpageo"   # lokale DB statt VPS
.venv/Scripts/python export_region.py --region "*" --out data/berlin --jobs 6
.venv/Scripts/python build_region.py data/berlin out/berlin --jobs 10
```

`--region "*"` exportiert ganz Berlin. Unter Git Bash `PYTHONIOENCODING=utf-8` setzen (Umlaute in der Ausgabe).
Der Export ist wiederaufnehmbar; jede Zelldatei endet mit der Prüfmarke `#END`.

**Datenbankzugang** nur über die Umgebungsvariable `PSQL_CMD` (Standard: `psql` im Container `enpageo_db`
auf der VPS über `ssh enpageo-vps`). Zieht die DB um, nur diese Variable ändern.

| Datei | Aufgabe |
|---|---|
| `export_cell.sql` | eine Zelle: 2D-Gebäude, Kandidaten (Verknüpfungstabelle, Überlappung) und LoD2-Flächen |
| `export_region.py` | Zellen der Region bestimmen, parallel exportieren (Standard 2 Abfragen) |
| `build_region.py` | Körper global zuordnen (bridge / spatial / covered / estimated), Kacheln + Attribute schreiben |
| `glb.py` | Triangulierung (mit Höfen), Extrusion, GLB-Writer (quantisiert, Feature-IDs, Metadaten) |

Ausgabe: `tileset.json`, `overview/` (4 km), `coarse/` (1 km), `fine/` (250 m, LoD2), `attrs/` (Attribute je
Zelle bzw. verschlankt je Übersichtskachel), `build_report.json`. Hintergrund und Messwerte:
`.segberss/enpageo/cesium-karte/PHASE0.md` und `PHASE1.md`.
