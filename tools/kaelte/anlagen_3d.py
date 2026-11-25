"""Kälteanlagen als 3D-Klötze: Umriss je Anlage auf das LoD2-Dach projiziert -> core_bldg.chiller_3d

Höhe = Dachhöhe über dem Gebäudefuß, so verschoben wie in den Kacheln (cesium-karte/pipeline: jedes Gebäude um sein
eigenes Minimum aller Flächen; flache Erde ohne Gelände). Die Karte setzt darauf einen Klotz mit waagerechtem Deckel 1 m
über der höchsten Dachstelle unter dem Umriss; die 1 m sind eine reine Darstellungshöhe, gemessen ist sie nicht.

placement: flat      Dachfläche mit weniger als 5 Grad Neigung
           pitched   geneigte Dachfläche, die Eckpunkte folgen der Dachebene
           estimated Gebäude ohne LoD2-Dach, Kachel zeigt einen Kasten mit Schätzhöhe
           ground    kein Dach darunter, steht auf dem Boden
Maßgebend ist die Dachfläche mit der größten Überdeckung; Eckpunkte außerhalb bekommen höchstens deren höchste bzw.
tiefste Stelle.

Nach jedem Update der Anlagenliste neu ausführen: python tools/kaelte/anlagen_3d.py [--dsn ...]
"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import psycopg

PITCHED_DEG = 5.0

FACETS = """
SELECT DISTINCT ON (c.objectid) c.objectid, ts.building_id, ST_AsGeoJSON(sg.geometry, 3), a.area / ST_Area(v.g), a.area
FROM core_bldg."Chillers" c
CROSS JOIN LATERAL (SELECT ST_MakeValid(c.geom) AS g) v
JOIN citydb.surface_geometry sg ON sg.geometry && c.geom AND sg.geometry IS NOT NULL
JOIN citydb.thematic_surface ts ON ts.lod2_multi_surface_id = sg.root_id AND ts.objectclass_id = 33
CROSS JOIN LATERAL (SELECT ST_Area(ST_Intersection(ST_MakeValid(ST_Force2D(sg.geometry)), v.g)) AS area) a
WHERE a.area > 0.05
ORDER BY c.objectid, round(a.area::numeric, 1) DESC, ST_ZMax(sg.geometry) DESC
"""
BODY_BASE = """
SELECT ts.building_id, min(ST_ZMin(sg.geometry))
FROM citydb.thematic_surface ts JOIN citydb.surface_geometry sg ON sg.root_id = ts.lod2_multi_surface_id
WHERE ts.building_id = ANY(%s) AND ts.objectclass_id IN (33, 34, 35) AND sg.geometry IS NOT NULL GROUP BY 1
"""
ESTIMATED = """
SELECT DISTINCT ON (c.objectid) c.objectid,
       GREATEST(3, LEAST(100, COALESCE(pb."Gross volume" / NULLIF(pb."Footprint area", 0),
                                       pb."Storey number" * COALESCE(pb."Average Storey Height", 3), 9)))
FROM core_bldg."Chillers" c
JOIN emc.building b ON b.geom && c.geom AND ST_Intersects(ST_MakeValid(b.geom), ST_PointOnSurface(ST_MakeValid(c.geom)))
LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
WHERE c.objectid = ANY(%s)
ORDER BY c.objectid, ST_Area(b.geom) DESC
"""


def plane(geojson):
    """Ebene z = a*x + b*y + c (um den Schwerpunkt), Höhenbereich und Neigung einer Dachfläche."""
    g = json.loads(geojson)
    ring = g["coordinates"][0] if g["type"] == "Polygon" else g["coordinates"][0][0]
    p = np.asarray(ring)[:, :3]
    o = p.mean(axis=0)
    q = p - o
    sol, *_ = np.linalg.lstsq(q[:, :2], q[:, 2], rcond=None)
    return o, sol, p[:, 2].min(), p[:, 2].max(), math.degrees(math.atan(float(np.hypot(*sol))))


def dsn_from_env_file() -> str:
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("DATABASE_URL nicht in .env gefunden, bitte --dsn angeben")


def tile_bases(cur, bodies):
    """Fuß des Kachelgebäudes je Körper: Minimum über alle Körper, die dasselbe 2D-Gebäude bekommen hat (Zuordnung wie
    export_surfaces.sql: 1:1 allein, sonst alle Teilkörper; jeder Körper an den Anspruch mit der größten Überdeckung)."""
    cur.execute("SELECT bldg_uuid, citygml_bldg_id, match_type, COALESCE(relative_intersection, 0) "
                "FROM emc.relation_bldg_citygml_v2 WHERE citygml_bldg_id IS NOT NULL")
    rel = cur.fetchall()
    one = {u for u, _b, m, _r in rel if m == "1:1"}
    owner = {}
    for u, b, m, r in rel:
        if (m == "1:1" or u not in one) and (b not in owner or r > owner[b][1]):
            owner[b] = (u, r)
    parts = defaultdict(set)
    for b, (u, _r) in owner.items():
        parts[u].add(b)
    group = {b: (parts[owner[b][0]] if b in owner else {b}) for b in bodies}
    cur.execute(BODY_BASE, (sorted(set().union(*group.values())),))
    z = dict(cur.fetchall())
    return {b: min(z[x] for x in g if x in z) for b, g in group.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    args = ap.parse_args()
    with psycopg.connect(args.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute('SELECT objectid, ST_AsGeoJSON(geom, 3) FROM core_bldg."Chillers"')
        outlines = {o: json.loads(g) for o, g in cur.fetchall()}
        cur.execute(FACETS)
        facets = {r[0]: r[1:] for r in cur.fetchall()}
        ids = sorted({f[0] for f in facets.values()})
        base = tile_bases(cur, ids)
        rest = [o for o in outlines if o not in facets]
        cur.execute(ESTIMATED, (rest,))
        est = {o: float(h) for o, h in cur.fetchall()}

        rows = []
        for oid, g in outlines.items():
            if oid in facets:
                body, facet, cover, _ = facets[oid]
                o, (a, b), lo, hi, deg = plane(facet)
                z0 = base[body]
                kind = "pitched" if deg >= PITCHED_DEG else "flat"
                z = lambda x, y: round(float(min(hi, max(lo, o[2] + a * (x - o[0]) + b * (y - o[1])))) - z0, 2)
            elif oid in est:
                kind, cover, deg = "estimated", None, None
                z = lambda x, y, h=est[oid]: round(h, 2)
            else:
                kind, cover, deg = "ground", None, None
                z = lambda x, y: 0.0
            polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
            coords = [[[[p[0], p[1], z(p[0], p[1])] for p in ring] for ring in poly] for poly in polys]
            zs = [p[2] for poly in coords for ring in poly for p in ring]
            rows.append((oid, kind, cover, deg, min(zs), max(zs), json.dumps({"type": "MultiPolygon", "coordinates": coords})))

        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.chiller_3d;
            CREATE TABLE core_bldg.chiller_3d (
                objectid int PRIMARY KEY, placement text NOT NULL, roof_cover double precision, roof_slope_deg double precision,
                z_min double precision, z_max double precision, geom geometry(MultiPolygonZ, 4326) NOT NULL);
            COMMENT ON TABLE core_bldg.chiller_3d IS
              'Umriss je Kälteanlage auf das LoD2-Dach projiziert, Höhe in m über dem Gebäudefuß (Kachelhöhe, flache Erde); '
              'tools/kaelte/anlagen_3d.py. Die Karte zieht daraus 1 m hohe Klötze (Darstellungshöhe, nicht gemessen).';""")
        cur.executemany("""INSERT INTO core_bldg.chiller_3d VALUES (%s,%s,%s,%s,%s,%s,
            ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833), 4326))""", rows)
        con.commit()
        cur.execute("SELECT placement, count(*), round(min(z_min)::numeric,1), round(max(z_max)::numeric,1), "
                    "round(avg(roof_cover)::numeric,2) FROM core_bldg.chiller_3d GROUP BY 1 ORDER BY 2 DESC")
        for r in cur.fetchall():
            print(*r)


if __name__ == "__main__":
    main()
