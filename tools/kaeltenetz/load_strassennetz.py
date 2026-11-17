"""Straßennetz (Detailnetz Berlin) als Trassen-Hauptnetz für Kälte-/Wärmenetze und kürzeste Anschlussleitung
je Gebäude.

  core_base.street_segment          Straßenabschnitte (gdi.berlin.de WFS detailnetz:c_strassenabschnitte)
  core_bldg.network_connection      je Gebäude: nächster geeigneter Straßenabschnitt, kürzeste Linie Straße -> Gebäude
  core_base.v_street_segment_load   Sicht: je Abschnitt angeschlossene Gebäude, Anschlusslängen, Wärme (KWW),
                                    Kälte (Platzhalter) und Ist-Kälte (erkannte Anlagen), Liniendichten

Hauptnetz = Straßenabschnitte auf Geländeebene (verkehrsebene = 0) ohne Autobahnen. Angeschlossen werden alle
beheizten Gebäude (core_bldg.kww_gfk_mapping.heated) und alle Gebäude mit erkannter Kälteanlage.

Aufruf: python load_strassennetz.py [--dsn ...] [--max-dist 150]
        python load_strassennetz.py --only-missing     (nach neuer Kälteanlagenliste, Minuten statt Stunden)
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


def add_missing_connections(dsn: str, max_dist: float):
    """Anschlussleitungen für Gebäude ergänzen, die neu beheizt gelten oder neu eine erkannte Kälteanlage tragen.
    Die volle Berechnung dauert ~5 h; nach einem Update der Anlagenliste reicht das hier."""
    t0 = time.perf_counter()
    with psycopg.connect(dsn) as con, con.cursor() as cur:
        cur.execute(f"""
            INSERT INTO core_bldg.network_connection
            WITH bl AS (
                SELECT b.uuid, b.geom FROM emc.building b
                JOIN core_bldg.kww_gfk_mapping k ON k.gfk = b.gfk
                WHERE k.heated
                UNION
                SELECT b.uuid, b.geom FROM emc.building b
                WHERE EXISTS (SELECT 1 FROM core_building."Chillers" c WHERE ST_Intersects(c.geom, b.geom))
                UNION
                SELECT b.uuid, b.geom FROM emc.building b
                WHERE b.uuid = ANY(ARRAY(SELECT uuid FROM core_bldg.cooling_estimate_building))
            )
            SELECT bl.uuid, s.id, s.street_name,
                   round(ST_Length(ST_ShortestLine(s.geom, bl.geom))::numeric, 1),
                   ST_ShortestLine(s.geom, bl.geom)::geometry(LineString, 25833)
            FROM bl
            CROSS JOIN LATERAL (
                SELECT s.id, s.street_name, s.geom FROM core_base.street_segment s
                WHERE s.is_main_network AND ST_DWithin(s.geom, bl.geom, {max_dist})
                ORDER BY s.geom <-> bl.geom LIMIT 1) s
            WHERE NOT EXISTS (SELECT 1 FROM core_bldg.network_connection n WHERE n.uuid = bl.uuid)""")
        n = cur.rowcount
        con.commit()
    print(f"{n} Anschlussleitungen ergänzt ({time.perf_counter() - t0:.0f} s)")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    ap.add_argument("--max-dist", type=float, default=150, help="maximale Anschlusslänge in m")
    ap.add_argument("--only-missing", action="store_true",
                    help="Straßennetz behalten, nur fehlende Anschlüsse (z. B. nach neuer Kälteanlagenliste) ergänzen")
    a = ap.parse_args()
    if a.only_missing:
        add_missing_connections(a.dsn or dsn_from_env_file(), a.max_dist)
        return
    rows = []
    for f in features("detailnetz:c_strassenabschnitte"):
        p = f["properties"]
        rows.append((p.get("element_nr"), p.get("strassenname"), p.get("strassenklasse"), p.get("strassenklasse1"),
                     p.get("strassenklasse2"), p.get("verkehrsebene"), p.get("bezirk"), p.get("laenge"),
                     json.dumps(f["geometry"])))
    t0 = time.perf_counter()
    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
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
        print("Anschlussleitungen werden berechnet …")
        cur.execute(f"""
            CREATE TABLE core_bldg.network_connection AS
            WITH bl AS (
                SELECT b.uuid, b.geom FROM emc.building b
                JOIN core_bldg.kww_gfk_mapping k ON k.gfk = b.gfk
                WHERE k.heated
                UNION
                SELECT b.uuid, b.geom FROM emc.building b
                WHERE EXISTS (SELECT 1 FROM core_building."Chillers" c WHERE ST_Intersects(c.geom, b.geom))
            )
            SELECT bl.uuid, s.id AS segment_id, s.street_name,
                   round(ST_Length(ST_ShortestLine(s.geom, bl.geom))::numeric, 1) AS length_m,
                   ST_ShortestLine(s.geom, bl.geom)::geometry(LineString, 25833) AS geom
            FROM bl
            CROSS JOIN LATERAL (
                SELECT s.id, s.street_name, s.geom FROM core_base.street_segment s
                WHERE s.is_main_network AND ST_DWithin(s.geom, bl.geom, {a.max_dist})
                ORDER BY s.geom <-> bl.geom LIMIT 1) s;
            ALTER TABLE core_bldg.network_connection ADD PRIMARY KEY (uuid);
            CREATE INDEX ON core_bldg.network_connection (segment_id);
            CREATE INDEX ON core_bldg.network_connection USING gist (geom);
            COMMENT ON TABLE core_bldg.network_connection IS
              'Kürzeste Anschlussleitung je Gebäude vom nächsten Hauptnetz-Straßenabschnitt (core_base.street_segment, '
              'is_main_network) bis zur Gebäudekante. Beheizte Gebäude und Gebäude mit erkannter Kälteanlage, max. {a.max_dist:.0f} m. '
              'Luftlinie ohne Hindernisse (Nachbargebäude, Gleise, Gewässer).';""")
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
        cur.execute("SELECT count(*), round(avg(length_m),1), percentile_cont(0.95) WITHIN GROUP (ORDER BY length_m) "
                    "FROM core_bldg.network_connection")
        n, avg, p95 = cur.fetchone()
        print(f"Anschlüsse: {n} Gebäude, mittlere Länge {avg} m, 95-%-Wert {p95:.0f} m ({time.perf_counter() - t0:.0f} s)")
        con.commit()


if __name__ == "__main__":
    main()
