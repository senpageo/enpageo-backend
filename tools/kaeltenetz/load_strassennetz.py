"""Straßennetz (Detailnetz Berlin) als Trassen-Hauptnetz für Kälte-/Wärmenetze und kürzeste Anschlussleitung
je Gebäude.

  core_base.street_segment          Straßenabschnitte (gdi.berlin.de WFS detailnetz:c_strassenabschnitte)
  core_bldg.network_connection      je Gebäude: nächster geeigneter Straßenabschnitt, kürzeste Linie Straße -> Gebäude
  core_base.v_street_segment_load   Sicht: je Abschnitt angeschlossene Gebäude, Anschlusslängen, Wärme (KWW),
                                    Kälte (Platzhalter) und Ist-Kälte (erkannte Anlagen), Liniendichten

  core_base.v_potential_network     Sicht: potenzielle Netzstruktur (Hauptleitungen + Anschlussleitungen)

Es geht nicht um bestehende Netze und nicht um die erkannten Kälteanlagen, sondern um eine potenzielle Netzstruktur
für die ganze Stadt: sobald Wärme- und Kältebedarf je Gebäude vorliegen, ergeben sich daraus Liniendichten, die zeigen,
wo Wärme- oder Kältenetze sinnvoll wären.

Hauptnetz = Straßenabschnitte auf Geländeebene (verkehrsebene = 0) ohne Autobahnen. Angeschlossen werden alle
beheizten Gebäude in Berlin (core_bldg.kww_gfk_mapping.heated, emc.building.displayed, Lage in emc.district),
jeweils an den nächsten Hauptnetz-Abschnitt, ohne Längengrenze (> --max-dist: beyond_max_dist).

Aufruf: python load_strassennetz.py [--dsn ...] [--max-dist 150]   (Straßennetz neu laden + alle Anschlüsse)
        python load_strassennetz.py --sync                        (Anschlüsse auf den Gebäudebestand abgleichen)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg

WFS = "https://gdi.berlin.de/services/wfs/detailnetz"


def features(typename: str, page: int = 20000):
    start = 0
    while True:
        q = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": typename,
             "OUTPUTFORMAT": "application/json", "COUNT": page, "STARTINDEX": start}
        req = urllib.request.Request(WFS + "?" + urllib.parse.urlencode(q), headers={"User-Agent": "enpageo-coolingmap/1.0"})
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read().decode("utf-8"))
        yield from d["features"]
        start += len(d["features"])
        print(f"  {typename}: {start} / {d.get('numberMatched')}")
        if not d["features"] or start >= int(d.get("numberMatched") or 0):
            break


def dsn_from_env_file() -> str:
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("DATABASE_URL nicht in .env gefunden, bitte --dsn angeben")


BUILDINGS = """
    SELECT b.uuid, b.geom FROM emc.building b
    JOIN core_bldg.kww_gfk_mapping k ON k.gfk = b.gfk AND k.heated
    WHERE b.displayed
      AND EXISTS (SELECT 1 FROM emc.district d WHERE ST_Intersects(d.geom, ST_PointOnSurface(b.geom)))"""


def sync_connections(dsn: str, max_dist: float):
    """Anschlussleitungen auf den Gebäudebestand abgleichen: alle beheizten Gebäude in Berlin (ALKIS-Gebäudefunktion,
    core_bldg.kww_gfk_mapping.heated) bekommen die kürzeste Leitung zum nächsten Hauptnetz-Abschnitt, ohne Längengrenze;
    länger als max_dist -> beyond_max_dist. Gebäude, die nicht mehr dazugehören, fallen heraus. Erkannte Kälteanlagen
    spielen keine Rolle: das Netz ist eine potenzielle Struktur für spätere Wärme- und Kälteliniendichten."""
    t0 = time.perf_counter()
    with psycopg.connect(dsn) as con, con.cursor() as cur:
        cur.execute("ALTER TABLE core_bldg.network_connection ADD COLUMN IF NOT EXISTS beyond_max_dist boolean")
        cur.execute(f"CREATE TEMP TABLE sel AS {BUILDINGS}")
        cur.execute("ALTER TABLE sel ADD PRIMARY KEY (uuid)")
        cur.execute("ANALYZE sel")
        cur.execute("DELETE FROM core_bldg.network_connection n WHERE NOT EXISTS (SELECT 1 FROM sel WHERE sel.uuid = n.uuid)")
        removed = cur.rowcount
        cur.execute("""
            INSERT INTO core_bldg.network_connection (uuid, segment_id, street_name, length_m, geom)
            SELECT bl.uuid, s.id, s.street_name,
                   round(ST_Length(ST_ShortestLine(s.geom, bl.geom))::numeric, 1),
                   ST_ShortestLine(s.geom, bl.geom)::geometry(LineString, 25833)
            FROM sel bl
            CROSS JOIN LATERAL (
                SELECT s.id, s.street_name, s.geom FROM core_base.street_segment s
                WHERE s.is_main_network ORDER BY s.geom <-> bl.geom LIMIT 1) s
            WHERE NOT EXISTS (SELECT 1 FROM core_bldg.network_connection n WHERE n.uuid = bl.uuid)""")
        added = cur.rowcount
        cur.execute("UPDATE core_bldg.network_connection SET beyond_max_dist = length_m > %s "
                    "WHERE beyond_max_dist IS DISTINCT FROM (length_m > %s)", (max_dist, max_dist))
        cur.execute(f"""
            COMMENT ON TABLE core_bldg.network_connection IS
              'Potenzielle Anschlussleitung je beheiztem Gebäude in Berlin: kürzeste Linie vom nächsten Hauptnetz-Abschnitt '
              '(core_base.street_segment, is_main_network) zur Gebäudekante, ohne Längengrenze (beyond_max_dist: länger als '
              '{max_dist:.0f} m). Luftlinie ohne Hindernisse (Nachbargebäude, Gleise, Gewässer). Unabhängig von bestehenden '
              'Netzen und erkannten Kälteanlagen – Grundlage für Wärme- und Kälteliniendichten.'""")
        cur.execute("""
            CREATE OR REPLACE VIEW core_base.v_potential_network AS
            SELECT 'hauptleitung'::text AS kind, s.id AS segment_id, NULL::text AS uuid, s.street_name, s.district,
                   s.length_m, n.n_buildings, false AS beyond_max_dist, s.geom
            FROM core_base.street_segment s
            JOIN (SELECT segment_id, count(*) AS n_buildings FROM core_bldg.network_connection GROUP BY 1) n
              ON n.segment_id = s.id
            UNION ALL
            SELECT 'anschlussleitung', c.segment_id, c.uuid::text, c.street_name, NULL, c.length_m, 1,
                   c.beyond_max_dist, c.geom
            FROM core_bldg.network_connection c""")
        cur.execute("""
            COMMENT ON VIEW core_base.v_potential_network IS
              'Potenzielle Wärme-/Kältenetzstruktur Berlin: Hauptleitungen = Hauptnetz-Straßenabschnitte, an denen mindestens '
              'ein beheiztes Gebäude anschließt; Anschlussleitungen je Gebäude (core_bldg.network_connection). Zeigt, wo Netze '
              'möglich wären, nicht bestehende Netze.'""")
        cur.execute("""SELECT count(*), count(*) FILTER (WHERE beyond_max_dist), round(avg(length_m), 1),
                              count(DISTINCT segment_id) FROM core_bldg.network_connection""")
        n, far, avg, segs = cur.fetchone()
        con.commit()
    print(f"Anschlüsse: {n} Gebäude (+{added}, -{removed}), davon {far} länger als {max_dist:.0f} m, "
          f"mittlere Länge {avg} m, {segs} Hauptleitungsabschnitte ({time.perf_counter() - t0:.0f} s)")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    ap.add_argument("--max-dist", type=float, default=150,
                    help="Anschlüsse länger als das (m) werden als beyond_max_dist markiert, nicht weggelassen")
    ap.add_argument("--sync", "--only-missing", dest="only_missing", action="store_true",
                    help="Straßennetz behalten, Anschlüsse nur auf den Gebäudebestand abgleichen (Minuten)")
    a = ap.parse_args()
    dsn = a.dsn or dsn_from_env_file()
    if a.only_missing:
        sync_connections(dsn, a.max_dist)
        return
    rows = []
    for f in features("detailnetz:c_strassenabschnitte"):
        p = f["properties"]
        rows.append((p.get("element_nr"), p.get("strassenname"), p.get("strassenklasse"), p.get("strassenklasse1"),
                     p.get("strassenklasse2"), p.get("verkehrsebene"), p.get("bezirk"), p.get("laenge"),
                     json.dumps(f["geometry"])))
    t0 = time.perf_counter()
    with psycopg.connect(dsn) as con, con.cursor() as cur:
        cur.execute("""
            CREATE SCHEMA IF NOT EXISTS core_base;
            DROP VIEW IF EXISTS core_base.v_street_segment_load;
            DROP TABLE IF EXISTS core_bldg.network_connection;
            DROP TABLE IF EXISTS core_base.street_segment;
            CREATE TABLE core_base.street_segment (
                id serial PRIMARY KEY, element_nr text, street_name text, class text, class1 text, class2 text,
                traffic_level int, district text, length_m numeric, is_main_network boolean,
                geom geometry(LineString, 25833));
            COMMENT ON TABLE core_base.street_segment IS
              'Straßenabschnitte Detailnetz Berlin (gdi.berlin.de WFS detailnetz:c_strassenabschnitte). is_main_network: '
              'Geländeebene (verkehrsebene 0), keine Autobahn – mögliche Trasse für Kälte-/Wärmenetze.';""")
        cur.executemany(
            "INSERT INTO core_base.street_segment (element_nr, street_name, class, class1, class2, traffic_level, "
            "district, length_m, geom) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, "
            "ST_LineMerge(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833)))", rows)
        cur.execute("""
            UPDATE core_base.street_segment SET is_main_network =
                coalesce(traffic_level, 0) = 0 AND coalesce(class2, '') NOT IN ('AUTO', 'BAB')
                AND coalesce(class, '') <> 'A';
            CREATE INDEX ON core_base.street_segment USING gist (geom);
            ANALYZE core_base.street_segment;""")
        cur.execute("SELECT class, class2, traffic_level, is_main_network, count(*) FROM core_base.street_segment "
                    "GROUP BY 1,2,3,4 ORDER BY 5 DESC LIMIT 15")
        for r in cur.fetchall():
            print("  ", r)
        cur.execute("""
            CREATE TABLE core_bldg.network_connection (
                uuid text PRIMARY KEY, segment_id int, street_name text, length_m numeric, beyond_max_dist boolean,
                geom geometry(LineString, 25833));
            CREATE INDEX ON core_bldg.network_connection (segment_id);
            CREATE INDEX ON core_bldg.network_connection USING gist (geom);""")
        cur.execute("""
            CREATE VIEW core_base.v_street_segment_load AS
            SELECT s.id, s.street_name, s.district, s.length_m, s.geom,
                   count(c.uuid) AS n_buildings,
                   round(sum(c.length_m), 1) AS connection_length_m,
                   round(sum(kh.heat_dhw_kwh_a) / 1000, 1) AS heat_kww_mwh_a,
                   round(sum(ce.cooling_kwh_mean) / 1000, 1) AS cooling_actual_mwh_a,
                   round(sum(ce.cooling_kwh_min) / 1000, 1) AS cooling_actual_min_mwh_a,
                   round(sum(ce.cooling_kwh_max) / 1000, 1) AS cooling_actual_max_mwh_a,
                   round(sum(kh.heat_dhw_kwh_a) / 1000 / nullif(s.length_m + coalesce(sum(c.length_m), 0), 0), 3)
                       AS heat_line_density_mwh_m_a,
                   round(sum(ce.cooling_kwh_mean) / 1000 / nullif(s.length_m + coalesce(sum(c.length_m), 0), 0), 3)
                       AS cooling_line_density_mwh_m_a
            FROM core_base.street_segment s
            LEFT JOIN core_bldg.network_connection c ON c.segment_id = s.id
            LEFT JOIN core_bldg.kww_heat_demand_building kh ON kh.uuid = c.uuid AND kh.scenario = '2025'
            LEFT JOIN core_bldg.cooling_estimate_building ce ON ce.uuid = c.uuid
            WHERE s.is_main_network
            GROUP BY s.id;
            COMMENT ON VIEW core_base.v_street_segment_load IS
              'Je Hauptnetz-Straßenabschnitt: angeschlossene Gebäude, Anschlusslängen, Wärme (KWW) und Ist-Kälte '
              '(erkannte Anlagen, mit Spanne) sowie Liniendichten in MWh je Trassenmeter (Abschnitt + Anschlüsse) und Jahr.';""")
        con.commit()
    print(f"Straßennetz geladen ({time.perf_counter() - t0:.0f} s), Anschlussleitungen werden berechnet …")
    sync_connections(dsn, a.max_dist)

if __name__ == "__main__":
    main()
