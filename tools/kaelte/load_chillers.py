"""Neue Kälteanlagenliste (KIT, Mask R-CNN, Esri-JSON mit Kälteleistungen) nach core_building."Chillers" laden.

Die bisherige Tabelle wird einmalig als core_building."Chillers_<suffix>" aufgehoben (Standard: Chillers_2025),
die neue Liste ersetzt core_building."Chillers". Spalten der alten Tabelle bleiben erhalten (confidence liefert die
neue Liste nicht -> NULL), neu sind:

  fans                     erkannte Lüfterzahl
  capacity_regression_kw   Kälteleistung aus der Regression (Barth et al.)
  capacity_kw              Mittelwert der Monte-Carlo-Simulation  -> Grundlage der Ist-Kälte-Schätzung
  capacity_sd_kw           Standardabweichung der Monte-Carlo-Simulation -> Band ±1 σ
  source                   Dateiname der Lieferung

Esri-Ringe: im Uhrzeigersinn = Außenring, gegen den Uhrzeigersinn = Loch (gehört zum vorhergehenden Außenring).

Aufruf: python load_chillers.py <datei.json> [--dsn ...] [--keep-as Chillers_2025]
Danach: python kaelte_ist_schaetzung.py  und  python ../kaeltenetz/load_strassennetz.py --only-missing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kaelte_ist_schaetzung import dsn_from_env_file  # noqa: E402


def signed_area(ring):
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2


def to_multipolygon(rings):
    polys = []
    for ring in rings:
        ring = [p[:2] for p in ring]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        if signed_area(ring) < 0 or not polys:  # Esri: clockwise = outer ring
            polys.append([ring])
        else:
            polys[-1].append(ring)
    return json.dumps({"type": "MultiPolygon", "coordinates": polys})


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--dsn")
    ap.add_argument("--keep-as", default="Chillers_2025")
    a = ap.parse_args()
    t0 = time.perf_counter()
    d = json.loads(Path(a.path).read_text(encoding="utf-8"))
    srid = d.get("spatialReference", {}).get("latestWkid") or d.get("spatialReference", {}).get("wkid")
    if srid != 25833:
        raise SystemExit(f"Erwartet EPSG:25833, Datei hat {srid}")
    rows = []
    for f in d["features"]:
        at = f["attributes"]
        rows.append((at["OBJECTID"], at["Class"], at.get("Fans"), at.get("TARGET_FID"),
                     at.get("Regression_Cooling_Capacity_kW"), at.get("Monte_Carlo_Mean_Capacity_kW"),
                     at.get("Monte_Carlo_St_Dev_Capacity_kW"), to_multipolygon(f["geometry"]["rings"])))
    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f'core_building."{a.keep_as}"',))
        if cur.fetchone()[0] is None:
            cur.execute(f'ALTER TABLE core_building."Chillers" RENAME TO "{a.keep_as}"')
            cur.execute(f'ALTER INDEX IF EXISTS core_building."Chillers_pkey" RENAME TO "{a.keep_as}_pkey"')
            cur.execute(f'ALTER INDEX IF EXISTS core_building.idx_chillers_geom RENAME TO "idx_{a.keep_as.lower()}_geom"')
            print(f'Bisherige Liste aufgehoben als core_building."{a.keep_as}"')
        else:
            cur.execute('DROP TABLE IF EXISTS core_building."Chillers"')
        cur.execute("""
            CREATE TABLE core_building."Chillers" (
                objectid integer PRIMARY KEY, class text, confidence double precision,
                fans integer, target_fid integer,
                shape_length double precision, shape_area double precision,
                capacity_regression_kw double precision, capacity_kw double precision, capacity_sd_kw double precision,
                source text, geom geometry(MultiPolygon, 25833))""")
        cur.execute("CREATE TEMP TABLE raw (objectid int, class text, fans int, target_fid int, reg float8, "
                    "mc float8, sd float8, gj text)")
        with cur.copy("COPY raw FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        cur.execute("""
            INSERT INTO core_building."Chillers"
            SELECT objectid, class, NULL, fans, target_fid, ST_Perimeter(g), ST_Area(g), reg, mc, sd, %s, g
            FROM (SELECT r.*, ST_Multi(ST_CollectionExtract(ST_MakeValid(
                      ST_SetSRID(ST_GeomFromGeoJSON(gj), 25833)), 3))::geometry(MultiPolygon, 25833) AS g FROM raw r) x""",
                    (Path(a.path).name,))
        cur.execute('CREATE INDEX idx_chillers_geom ON core_building."Chillers" USING gist (geom)')
        cur.execute("""COMMENT ON TABLE core_building."Chillers" IS
            'Kälteanlagen aus Luftbilderkennung (KIT, Mask R-CNN 2026) mit Kälteleistung: capacity_regression_kw '
            '(Regression), capacity_kw = Monte-Carlo-Mittel, capacity_sd_kw = Monte-Carlo-Standardabweichung. '
            'Vorgängerliste ohne Leistungen: core_building."Chillers_2025".'""")
        cur.execute("""SELECT class, count(*), round(sum(capacity_kw)/1000), round(sum(capacity_regression_kw)/1000)
                       FROM core_building."Chillers" GROUP BY 1 ORDER BY 3 DESC""")
        for c, n, mc, reg in cur.fetchall():
            print(f"  {c:22s} {n:5d} Anlagen  {mc:6} MW (Monte Carlo)  {reg:6} MW (Regression)")
        con.commit()
    print(f"{len(rows)} Anlagen geladen ({time.perf_counter() - t0:.0f} s)")


if __name__ == "__main__":
    main()
