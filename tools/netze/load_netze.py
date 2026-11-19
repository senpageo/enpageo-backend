"""Bestehende Wärme- und Kältenetze in Berlin in die Datenbank laden.

Wärme (gdi.berlin.de, Datenlizenz Deutschland Zero 2.0):
  core_base.heat_supply_cell      Fern- und Nahwärmeversorgung 2022, 200-m-Raster (WFS waermenetze:waerme_fern_nah_2022;
                                  eine Zelle gilt als versorgt, sobald eine versorgte Adresse darin liegt; Fern- und
                                  Nahwärme nicht getrennt, Leitungen nicht öffentlich)
  core_base.local_heat_network    Nahwärmenetze je Teilblock mit Ansprechpartner/Betreiber
                                  (WFS waermeplanung:bu_nahwaermenetz, Bestandsanalyse Wärmeplan Berlin)
Kälte (keine amtlichen Geodaten; von Hand aus öffentlichen Angaben):
  core_base.cooling_network       je Netz Betreiber, Leistung, Jahresarbeit, Trassenlänge, Temperaturen, Quelle;
                                  Kältezentrale als Gebäude, ungefähres Versorgungsgebiet = Hülle um die genannten
                                  Kunden (keine Trasse, die Leitungsverläufe sind nicht öffentlich)

Aufruf: python load_netze.py [--dsn ...]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kaeltenetz"))
from load_strassennetz import dsn_from_env_file  # noqa: E402

WFS = "https://gdi.berlin.de/services/wfs/{svc}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES={layer}" \
      "&SRSNAME=EPSG:25833&OUTPUTFORMAT=application/json"
UA = {"User-Agent": "enpageo-coolingmap/1.0"}

# Öffentliche Angaben je Kältenetz. Kunden = im Faktenblatt genannte Gebäude (OSM-Namen für die Ortssuche).
COOLING_NETWORKS = [
    {
        "name": "Quartier-Kälte Potsdamer Platz",
        "operator": "BEW Berliner Energie und Wärme GmbH (früher Vattenfall Wärme Berlin)",
        "capacity_mw": 50, "energy_mwh_a": 60000, "length_km": 14, "n_stations": 72,
        "t_supply_c": 6, "t_return_c": 12, "since": 1997,
        "plant": "Kältezentrale Stresemannstraße 122",
        "plant_query": "Kältezentrale Stresemannstraße Berlin",
        "customers": ["Sony Center Berlin", "Potsdamer Platz Arkaden", "Mall of Berlin", "Kulturforum Berlin",
                      "Abgeordnetenhaus von Berlin", "Bundesministerium für Umwelt Stresemannstraße 128 Berlin"],
        "remark": "Absorptions- (aus Fernwärme HKW Mitte) und Kompressionskälte; ca. 14 km kombinierte Wärme- und "
                  "Kältetrasse in Ringform; versorgt rund 12.000 Büros und 1.000 Wohnungen. Laut Presseberichten nutzt "
                  "eine Hochtemperatur-Wärmepumpe (8 MW) die Abwärme des Netzes für Fernwärme.",
        "source": "Vattenfall, Faktenblatt Kältezentrale Potsdamer Platz, Stand Juni 2017",
        "source_url": "https://group.vattenfall.com/de/siteassets/2.-dokumentablage---newsroom/3.-pressemeldungen/"
                      "Vattenfall-Faktenblatt-Kaeltezentrale-Potsdamer-Platz.pdf",
    },
]


def wfs(svc: str, layer: str) -> list[dict]:
    r = httpx.get(WFS.format(svc=svc, layer=layer), timeout=300, headers=UA)
    r.raise_for_status()
    try:
        text = r.content.decode("utf-8")
    except UnicodeDecodeError:  # the GeoServer answers some layers in Latin-1
        text = r.content.decode("latin-1")
    return json.loads(text)["features"]


def nominatim(q: str) -> dict | None:
    time.sleep(1.1)  # usage policy: at most one request per second
    r = httpx.get("https://nominatim.openstreetmap.org/search", headers=UA, timeout=20,
                  params={"q": q, "format": "jsonv2", "limit": 3, "polygon_geojson": 1,
                          "viewbox": "13.08,52.68,13.76,52.33", "bounded": 1})
    hits = [h for h in r.json() if h.get("category") not in ("highway", "railway", "public_transport")]
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    a = ap.parse_args()
    cells = wfs("waermenetze", "waermenetze:waerme_fern_nah_2022")
    local = wfs("waermeplanung", "waermeplanung:bu_nahwaermenetz")
    print(f"WFS: {len(cells)} versorgte 200-m-Zellen, {len(local)} Teilblöcke mit Nahwärmenetz")

    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute("""
            CREATE SCHEMA IF NOT EXISTS core_base;
            DROP TABLE IF EXISTS core_base.heat_supply_cell;
            DROP TABLE IF EXISTS core_base.local_heat_network;
            DROP TABLE IF EXISTS core_base.cooling_network;
            CREATE TABLE core_base.heat_supply_cell (id text PRIMARY KEY, category text,
                geom geometry(MultiPolygon, 25833));
            CREATE TABLE core_base.local_heat_network (schl5 text PRIMARY KEY, blocknr text, operator text,
                geom geometry(MultiPolygon, 25833));
            CREATE TABLE core_base.cooling_network (id serial PRIMARY KEY, name text, operator text, capacity_mw numeric,
                energy_mwh_a numeric, length_km numeric, n_stations int, t_supply_c numeric, t_return_c numeric,
                since int, plant text, customers text[], remark text, source text, source_url text,
                plant_geom geometry(Geometry, 25833), area_geom geometry(Geometry, 25833));""")
        cur.executemany(
            "INSERT INTO core_base.heat_supply_cell VALUES (%s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833)))",
            [(f["properties"]["id"], f["properties"]["kategorie"], json.dumps(f["geometry"])) for f in cells])
        cur.executemany(
            "INSERT INTO core_base.local_heat_network VALUES (%s, %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833))) "
            "ON CONFLICT (schl5) DO NOTHING",
            [(f["properties"]["schl5"], f["properties"].get("blocknr"), (f["properties"].get("ansprechpartner") or "").strip() or None,
              json.dumps(f["geometry"])) for f in local])
        for n in COOLING_NETWORKS:
            plant = nominatim(n["plant_query"])
            found = [h for h in (nominatim(c) for c in n["customers"]) if h]
            print(f"{n['name']}: Kältezentrale {'gefunden' if plant else 'NICHT gefunden'}, "
                  f"{len(found)} von {len(n['customers'])} Kunden gefunden")
            geoms = [json.dumps(h["geojson"]) for h in found]
            cur.execute("""
                INSERT INTO core_base.cooling_network (name, operator, capacity_mw, energy_mwh_a, length_km, n_stations,
                    t_supply_c, t_return_c, since, plant, customers, remark, source, source_url, plant_geom, area_geom)
                VALUES (%(name)s, %(operator)s, %(capacity_mw)s, %(energy_mwh_a)s, %(length_km)s, %(n_stations)s,
                        %(t_supply_c)s, %(t_return_c)s, %(since)s, %(plant)s, %(customers)s, %(remark)s, %(source)s,
                        %(source_url)s,
                        (SELECT b.geom FROM emc.building b
                          ORDER BY b.geom <-> ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833) LIMIT 1),
                        (SELECT ST_Buffer(ST_ConvexHull(ST_Collect(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(g), 4326), 25833))), 50)
                           FROM unnest(%(geoms)s::text[]) g))""",
                {**n, "lon": float(plant["lon"]) if plant else None, "lat": float(plant["lat"]) if plant else None,
                 "geoms": geoms})
        for t in ("heat_supply_cell", "local_heat_network", "cooling_network"):
            cur.execute(f"CREATE INDEX ON core_base.{t} USING gist ({'plant_geom' if t == 'cooling_network' else 'geom'})")
        cur.execute("""
            COMMENT ON TABLE core_base.heat_supply_cell IS 'Fern- und Nahwärmeversorgung 2022 als 200-m-Raster (gdi.berlin.de '
              'waermenetze:waerme_fern_nah_2022, dl-de/zero-2-0); Zelle versorgt, sobald eine versorgte Adresse darin liegt.';
            COMMENT ON TABLE core_base.local_heat_network IS 'Nahwärmenetze je Teilblock mit Ansprechpartner (gdi.berlin.de '
              'waermeplanung:bu_nahwaermenetz, Bestandsanalyse Wärmeplan Berlin 2026, dl-de/zero-2-0).';
            COMMENT ON TABLE core_base.cooling_network IS 'Bestehende Kältenetze aus öffentlichen Angaben (Quelle je Zeile); '
              'plant_geom = Gebäude der Kältezentrale, area_geom = Hülle um die genannten Kunden (+50 m), keine Trasse.';""")
        cur.execute("SELECT name, ST_Area(area_geom)::int, ST_AsText(ST_Centroid(plant_geom)) FROM core_base.cooling_network")
        print(cur.fetchall())
        con.commit()


if __name__ == "__main__":
    main()
