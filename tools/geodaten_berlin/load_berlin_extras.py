"""Freie Zusatzdaten für Ask the Map (Berlin): PV-Potenzial, PV-Bestand, Landeseigentum, Eigentumsstruktur,
Rechenzentren. Lädt die Quellen, legt Tabellen in core_bldg (gebäudebezogen) und core_base (gebietsbezogen)
neu an und ordnet sie den Gebäuden in emc.building zu.

Quellen (alle frei, Stand beim Laden in den Tabellenkommentaren):
  - Solarpotenzial Solarrechner, Dachflächen (gdi.berlin.de WFS ua_solarpotenzial_solarrechner), uid_gebaeude = ALKIS-ID
  - Solaranlagen PV, Umweltatlas (WFS ua_solaranlagen_pv): Anlagen > 30 kWp, Anlagen der öffentlichen Hand,
    Anlagen <= 30 kWp je PLZ
  - Berliner Landesgrundvermögen (WFS landesgrundvermoegen): Flurstücke des Landes Berlin
  - Eigentumskonzentration Berlin 2025 (WFS eigentumstruktur): Anteile je Planungsraum
  - Rechenzentren: OpenStreetMap (Overpass, telecom/building=data_center, ODbL) und PeeringDB (Facilities Berlin)

Aufruf:  python load_berlin_extras.py [--dsn ...] [--nur pv_potenzial,pv_bestand,eigentum,rechenzentren]
Ohne --dsn: DATABASE_URL aus backend-cesium/.env. Benötigt psycopg (v3).
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

UA = {"User-Agent": "enpageo-coolingmap/1.0"}
GDI = "https://gdi.berlin.de/services/wfs/"


def http_json(url: str, data: bytes | None = None, timeout: int = 300):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - network hiccups: retry a few times
            if attempt == 3:
                raise
            print(f"  Wiederholung nach Fehler: {e}")
            time.sleep(5 * (attempt + 1))


def wfs_features(service: str, typename: str, props: list[str] | None = None, page: int = 50000):
    """All features of a WFS layer as GeoJSON dicts (EPSG:25833), paged."""
    start = 0
    while True:
        q = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": f"{service}:{typename}",
             "OUTPUTFORMAT": "application/json", "COUNT": page, "STARTINDEX": start, "SORTBY": "id" if props and "id" in props else None}
        if props:
            q["PROPERTYNAME"] = ",".join(props)
        q = {k: v for k, v in q.items() if v is not None}
        d = http_json(GDI + service + "?" + urllib.parse.urlencode(q))
        feats = d.get("features", [])
        yield from feats
        start += len(feats)
        print(f"  {typename}: {start} / {d.get('numberMatched')}")
        if not feats or start >= int(d.get("numberMatched") or 0):
            break


def geom_json(f) -> str | None:
    return json.dumps(f["geometry"]) if f.get("geometry") else None


# ---------------------------------------------------------------------------------------------- PV-Potenzial
def load_pv_potential(con):
    agg: dict[str, list] = {}
    for f in wfs_features("ua_solarpotenzial_solarrechner", "d_photovoltaik_potenzial",
                          ["id", "uid_gebaeude", "potenzielle_leistung", "eignung", "flaeche", "geeignete_flaeche"],
                          page=100000):
        p = f["properties"]
        uid = p.get("uid_gebaeude")
        if not uid:
            continue
        a = agg.setdefault(uid, [0, 0.0, 0.0, 0.0, 0])  # n_flaechen, dachflaeche, geeignete_flaeche, kWp, n_geeignet
        a[0] += 1
        a[1] += p.get("flaeche") or 0
        if (p.get("eignung") or 0) > 0:
            a[2] += p.get("geeignete_flaeche") or p.get("flaeche") or 0
            a[3] += p.get("potenzielle_leistung") or 0
            a[4] += 1
    with con.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.pv_potential_building;
            CREATE TABLE core_bldg.pv_potential_building (
                uuid varchar PRIMARY KEY, n_roof_surfaces int, roof_area_m2 numeric, suitable_area_m2 numeric,
                potential_kwp numeric, n_suitable_surfaces int);
            COMMENT ON TABLE core_bldg.pv_potential_building IS
              'PV-Potenzial je Gebäude: Summe der geeigneten Dachflächen (eignung > 0) aus Solarrechner Berlin, '
              'WFS ua_solarpotenzial_solarrechner:d_photovoltaik_potenzial, uid_gebaeude = ALKIS-ID. Datenlizenz Deutschland Zero 2.0. '
              'Geladen mit tools/geodaten_berlin/load_berlin_extras.py';""")
        with cur.copy("COPY core_bldg.pv_potential_building FROM STDIN") as cp:
            for uid, a in agg.items():
                cp.write_row((uid, a[0], round(a[1], 1), round(a[2], 1), round(a[3], 2), a[4]))
    con.commit()
    print(f"PV-Potenzial: {len(agg)} Gebäude")


# ---------------------------------------------------------------------------------------------- PV-Bestand
def load_pv_installations(con):
    rows = []
    for layer, kind in (("a_sp08_09_1pvstandgr30kw", "gr30kwp"), ("c_sp08_09_1pvstand_oeh", "oeffentliche_hand")):
        for f in wfs_features("ua_solaranlagen_pv", layer):
            p = f["properties"]
            rows.append((kind, p.get("importid"), p.get("strasse_flur_lang"), str(p.get("plz") or ""),
                         p.get("inst_leistung"), p.get("jahr_inbetriebnahme") or p.get("inbetriebnahme"),
                         p.get("enh_nutzungsbereich") or p.get("nutzung_liegenschaft"),
                         p.get("institution"), p.get("institutionsgruppe"), p.get("name_liegenschaft"),
                         json.dumps(p, ensure_ascii=False), geom_json(f)))
    small = []
    for f in wfs_features("ua_solaranlagen_pv", "b_sp08_09_1pvstandkl30kw"):
        p = f["properties"]
        small.append((str(p.get("plz")), p.get("anzahl"), p.get("inst_leistung"), p.get("steckersolargeraet_anzahl"),
                      p.get("steckersolargeraet_leistung")))
    with con.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.pv_installation;
            CREATE TABLE core_bldg.pv_installation (
                id serial PRIMARY KEY, kind text NOT NULL, import_id int, address text, plz text,
                installed_kwp numeric, commissioning_year int, usage_area text, institution text,
                institution_group text, site_name text, attrs jsonb, geom geometry(MultiPoint, 25833),
                bldg_uuid varchar, match_method text);
            COMMENT ON TABLE core_bldg.pv_installation IS
              'PV-Bestandsanlagen, Umweltatlas Berlin (WFS ua_solaranlagen_pv, Basis Marktstammdatenregister): '
              'kind gr30kwp = Anlagen > 30 kWp, oeffentliche_hand = Anlagen auf Liegenschaften der öffentlichen Hand. '
              'Beide Ebenen überschneiden sich (gleiche import_id). bldg_uuid: Gebäude, das den Punkt enthält, sonst nächstes <= 30 m.';
            DROP TABLE IF EXISTS core_base.pv_small_plz;
            CREATE TABLE core_base.pv_small_plz (
                plz text PRIMARY KEY, n_installations int, installed_kwp numeric,
                n_plugin_devices int, plugin_kwp numeric);
            COMMENT ON TABLE core_base.pv_small_plz IS
              'PV-Anlagen bis 30 kWp, je Postleitzahl zusammengefasst (Umweltatlas Berlin, WFS ua_solaranlagen_pv). '
              'Nicht gebäudegenau, weil das Marktstammdatenregister kleine Anlagen ohne Standort veröffentlicht.';""")
        cur.executemany(
            "INSERT INTO core_bldg.pv_installation (kind, import_id, address, plz, installed_kwp, commissioning_year, "
            "usage_area, institution, institution_group, site_name, attrs, geom) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833)))",
            [(r[0], r[1], (r[2] or "").strip() or None, r[3], r[4],
              int(str(r[5])[-4:]) if r[5] and str(r[5])[-4:].isdigit() else None, *r[6:]) for r in rows])
        cur.executemany("INSERT INTO core_base.pv_small_plz VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", small)
        _match_points(cur, "core_bldg.pv_installation", 30)
        cur.execute("CREATE INDEX ON core_bldg.pv_installation (bldg_uuid)")
        # The two layers overlap (different import_id spaces): a public-sector entry is a duplicate when a > 30 kWp
        # entry has the same address and capacity.
        cur.execute("""
            UPDATE core_bldg.pv_installation SET attrs = attrs || '{"duplicate_of_gr30kwp": true}'
            WHERE id IN (SELECT c.id FROM core_bldg.pv_installation c JOIN core_bldg.pv_installation a
                           ON a.kind = 'gr30kwp' AND c.kind = 'oeffentliche_hand' AND a.address = c.address
                          AND abs(a.installed_kwp - c.installed_kwp) < 0.5);
            DROP TABLE IF EXISTS core_bldg.pv_installed_building;
            CREATE TABLE core_bldg.pv_installed_building AS
            SELECT bldg_uuid AS uuid, count(*) AS n_installations, round(sum(installed_kwp), 2) AS installed_kwp,
                   bool_or(kind = 'oeffentliche_hand' OR usage_area = 'Öffentliches Gebäude') AS public_sector,
                   string_agg(DISTINCT institution, '; ') AS institutions,
                   max(commissioning_year) AS latest_year
            FROM core_bldg.pv_installation
            WHERE bldg_uuid IS NOT NULL AND NOT coalesce((attrs->>'duplicate_of_gr30kwp')::boolean, false)
            GROUP BY bldg_uuid;
            ALTER TABLE core_bldg.pv_installed_building ADD PRIMARY KEY (uuid);
            COMMENT ON TABLE core_bldg.pv_installed_building IS
              'Installierte PV-Leistung je Gebäude aus core_bldg.pv_installation (Anlagen > 30 kWp und Anlagen der '
              'öffentlichen Hand, Doppelungen entfernt). Kleine private Anlagen fehlen (nur je PLZ in core_base.pv_small_plz).';""")
    con.commit()
    print(f"PV-Bestand: {len(rows)} Anlagenpunkte, {len(small)} PLZ")


def _match_points(cur, table: str, max_dist: float):
    """Point -> building: containing ALKIS building, else nearest within max_dist metres."""
    cur.execute(f"""
        UPDATE {table} t SET bldg_uuid = b.uuid, match_method = 'enthalten'
        FROM emc.building b
        WHERE b.gfk IS NOT NULL AND ST_Contains(b.geom, ST_GeometryN(t.geom, 1))""")
    cur.execute(f"""
        UPDATE {table} t SET bldg_uuid = n.uuid, match_method = 'naechstes_' || round(n.d)::int || 'm'
        FROM (
            SELECT t2.ctid AS rid, nb.uuid, nb.d
            FROM {table} t2
            CROSS JOIN LATERAL (
                SELECT b.uuid, ST_Distance(b.geom, ST_GeometryN(t2.geom, 1)) AS d
                FROM emc.building b
                WHERE b.gfk IS NOT NULL AND b.gfk NOT IN (2463, 2465, 2723)
                  AND ST_DWithin(b.geom, ST_GeometryN(t2.geom, 1), {max_dist})
                ORDER BY b.geom <-> ST_GeometryN(t2.geom, 1) LIMIT 1) nb
            WHERE t2.bldg_uuid IS NULL) n
        WHERE t.ctid = n.rid""")


# ---------------------------------------------------------------------------------------------- Eigentum
OWNER_LAYERS = {"e_fme_bezirk": "Land Berlin – Bezirksverwaltung",
                "d_fme_haupt": "Land Berlin – Hauptverwaltung",
                "f_andere_bereiche": "Land Berlin – Sondervermögen/Liegenschaftsfonds"}


def load_ownership(con):
    with con.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.state_property_parcel;
            CREATE TABLE core_bldg.state_property_parcel (
                id serial PRIMARY KEY, owner_class text NOT NULL, layer text, parcel_uuid text, bezirk text,
                bereich text, vermoegensstelle text, geom geometry(MultiPolygon, 25833));
            COMMENT ON TABLE core_bldg.state_property_parcel IS
              'Flurstücke im Eigentum des Landes Berlin (Berliner Landesgrundvermögen, gdi.berlin.de WFS landesgrundvermoegen): '
              'Vermögensstellen der Bezirks- und Hauptverwaltungen, Liegenschaftsfonds, SILB, SODA.';""")
        n = 0
        for layer, cls in OWNER_LAYERS.items():
            batch = []
            for f in wfs_features("landesgrundvermoegen", layer):
                p = f["properties"]
                batch.append((cls, layer, p.get("uuid"), p.get("bezirk"), p.get("bereich"), p.get("vermoegensstelle"),
                              geom_json(f)))
            cur.executemany(
                "INSERT INTO core_bldg.state_property_parcel (owner_class, layer, parcel_uuid, bezirk, bereich, "
                "vermoegensstelle, geom) VALUES (%s,%s,%s,%s,%s,%s, "
                "ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833)), 3)))", batch)
            n += len(batch)
        cur.execute("CREATE INDEX ON core_bldg.state_property_parcel USING gist (geom)")
        cur.execute("ANALYZE core_bldg.state_property_parcel")
        print(f"Landesgrundvermögen: {n} Flurstücke, Zuordnung zu Gebäuden läuft …")
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.building_ownership;
            CREATE TABLE core_bldg.building_ownership AS
            WITH ov AS (
                SELECT b.uuid, s.owner_class, s.vermoegensstelle, s.bezirk,
                       ST_Area(ST_Intersection(b.geom, s.geom)) / NULLIF(ST_Area(b.geom), 0) AS share
                FROM emc.building b
                JOIN core_bldg.state_property_parcel s ON ST_Intersects(b.geom, s.geom)
                WHERE b.gfk IS NOT NULL
            )
            SELECT DISTINCT ON (uuid) uuid, owner_class, vermoegensstelle, bezirk, round(share::numeric, 3) AS share
            FROM ov WHERE share >= 0.5
            ORDER BY uuid, share DESC;
            ALTER TABLE core_bldg.building_ownership ADD PRIMARY KEY (uuid);
            COMMENT ON TABLE core_bldg.building_ownership IS
              'Gebäude, deren Grundfläche zu mindestens 50 % auf einem Flurstück des Landes Berlin liegt (aus '
              'core_bldg.state_property_parcel). Fehlt ein Gebäude hier, ist es nicht landeseigen ODER die Angabe fehlt '
              '(Bund, landeseigene Gesellschaften, Kirchen, Private sind gebäudegenau nicht öffentlich).';""")
        cur.execute("SELECT owner_class, count(*) FROM core_bldg.building_ownership GROUP BY 1 ORDER BY 2 DESC")
        for r in cur.fetchall():
            print(f"  {r[0]}: {r[1]} Gebäude")

        # Planungsraum-Anteile (nur gebietsweise)
        cur.execute("""
            DROP TABLE IF EXISTS core_base.ownership_structure_pr;
            CREATE TABLE core_base.ownership_structure_pr (
                layer text, title text, attrs jsonb, geom geometry(MultiPolygon, 25833));
            COMMENT ON TABLE core_base.ownership_structure_pr IS
              'Eigentumskonzentration Berlin 2025 (gdi.berlin.de WFS eigentumstruktur): Anteil der Fläche je Eigentümergruppe '
              'auf Ebene der Planungsräume. Nicht gebäudegenau.';""")
        titles = {"a_eigentum_einzel_2025": "EinzeleigentümerInnen (natürliche Personen)",
                  "b_eigentum_jurist_privat_2025": "Juristische Personen (des privaten Rechts)",
                  "c_eigentum_stadtwohn_2025": "Städtische Wohnungsbaugesellschaften",
                  "d_eigentum_genossen_2025": "Genossenschaften",
                  "e_eigentum_gemeinwohl_2025": "Gemeinwohlorientierte EigentümerInnen",
                  "f_eigentum_jurist_2025": "Juristische Personen ohne städtische WBG und Genossenschaften"}
        for layer, title in titles.items():
            batch = [(layer, title, json.dumps(f["properties"], ensure_ascii=False), geom_json(f))
                     for f in wfs_features("eigentumstruktur", layer)]
            cur.executemany(
                "INSERT INTO core_base.ownership_structure_pr VALUES (%s,%s,%s, "
                "ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833)), 3)))", batch)
        cur.execute("CREATE INDEX ON core_base.ownership_structure_pr USING gist (geom)")
    con.commit()


# ---------------------------------------------------------------------------------------------- Rechenzentren
OVERPASS = ('[out:json][timeout:120];area["boundary"="administrative"]["admin_level"="4"]["name"="Berlin"]->.a;'
            '(nwr["telecom"="data_center"](area.a);nwr["building"="data_center"](area.a);'
            'nwr["telecom"="data_centre"](area.a);nwr["man_made"="data_center"](area.a););out geom tags;')


def load_data_centers(con):
    osm = http_json("https://overpass-api.de/api/interpreter", urllib.parse.urlencode({"data": OVERPASS}).encode())
    pdb = http_json("https://www.peeringdb.com/api/fac?country=DE&city__contains=Berlin")["data"]
    rows = []
    for e in osm["elements"]:
        t = e.get("tags", {})
        if e["type"] == "node":
            g = {"type": "Point", "coordinates": [e["lon"], e["lat"]]}
        elif e["type"] == "way" and e.get("geometry"):
            ring = [[p["lon"], p["lat"]] for p in e["geometry"]]
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            g = {"type": "Polygon", "coordinates": [ring]}
        else:  # relations: use bounds centre
            b = e.get("bounds")
            if not b:
                continue
            g = {"type": "Point", "coordinates": [(b["minlon"] + b["maxlon"]) / 2, (b["minlat"] + b["maxlat"]) / 2]}
        addr = " ".join(filter(None, [t.get("addr:street"), t.get("addr:housenumber")])) or None
        rows.append(("osm", f"{e['type']}/{e['id']}", t.get("name"), t.get("operator"), addr, json.dumps(g)))
    for f in pdb:
        if f.get("latitude") and f.get("longitude") and f.get("status") == "ok":
            g = {"type": "Point", "coordinates": [f["longitude"], f["latitude"]]}
            rows.append(("peeringdb", str(f["id"]), f["name"], f.get("org_name"),
                         f"{f.get('address1', '')}, {f.get('zipcode', '')} {f.get('city', '')}".strip(", "), json.dumps(g)))
    with con.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.data_center;
            CREATE TABLE core_bldg.data_center (
                id serial PRIMARY KEY, source text, source_id text, name text, operator text, address text,
                geom geometry(Geometry, 25833), bldg_uuid varchar, match_method text);
            COMMENT ON TABLE core_bldg.data_center IS
              'Rechenzentren in Berlin aus OpenStreetMap (telecom/building=data_center, ODbL) und PeeringDB (Facilities). '
              'Nicht vollständig: nur gemeldete bzw. kartierte Standorte. bldg_uuid: OSM-Gebäudeumriss mit >= 30 % Überdeckung, '
              'sonst Gebäude, das den Punkt enthält, sonst nächstes <= 50 m.';""")
        cur.executemany(
            "INSERT INTO core_bldg.data_center (source, source_id, name, operator, address, geom) VALUES "
            "(%s,%s,%s,%s,%s, ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 25833))", rows)
        # OSM-Umrisse: alle ALKIS-Gebäude mit >= 30 % Überdeckung (ein Umriss kann mehrere ALKIS-Gebäude treffen)
        cur.execute("""
            CREATE TEMP TABLE dc_poly AS
            SELECT d.id, b.uuid, ST_Area(ST_Intersection(b.geom, d.geom)) / NULLIF(ST_Area(b.geom), 0) AS share
            FROM core_bldg.data_center d JOIN emc.building b ON ST_Intersects(b.geom, d.geom)
            WHERE ST_GeometryType(d.geom) = 'ST_Polygon' AND b.gfk IS NOT NULL;
            DELETE FROM dc_poly WHERE share < 0.3;
            UPDATE core_bldg.data_center d SET bldg_uuid = p.uuid, match_method = 'umriss_' || round(p.share * 100)::int || 'pct'
            FROM (SELECT DISTINCT ON (id) id, uuid, share FROM dc_poly ORDER BY id, share DESC) p WHERE p.id = d.id;""")
        cur.execute("""
            UPDATE core_bldg.data_center d SET bldg_uuid = b.uuid, match_method = 'enthalten'
            FROM emc.building b
            WHERE d.bldg_uuid IS NULL AND b.gfk IS NOT NULL AND ST_Contains(b.geom, ST_PointOnSurface(d.geom))""")
        cur.execute("""
            UPDATE core_bldg.data_center d SET bldg_uuid = n.uuid, match_method = 'naechstes_' || round(n.dist)::int || 'm'
            FROM (SELECT d2.id, nb.uuid, nb.dist FROM core_bldg.data_center d2
                  CROSS JOIN LATERAL (SELECT b.uuid, ST_Distance(b.geom, d2.geom) AS dist FROM emc.building b
                                      WHERE b.gfk IS NOT NULL AND b.gfk NOT IN (2463, 2465, 2723, 2523)
                                        AND ST_DWithin(b.geom, d2.geom, 50)
                                      ORDER BY b.geom <-> ST_PointOnSurface(d2.geom) LIMIT 1) nb
                  WHERE d2.bldg_uuid IS NULL) n
            WHERE n.id = d.id""")
        # Gebäudeliste: alle getroffenen Gebäude, Namen und Quellen zusammengefasst
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.data_center_building;
            CREATE TABLE core_bldg.data_center_building AS
            WITH hits AS (
                SELECT bldg_uuid AS uuid, id FROM core_bldg.data_center WHERE bldg_uuid IS NOT NULL
                UNION
                SELECT uuid, id FROM dc_poly)
            SELECT h.uuid,
                   string_agg(DISTINCT coalesce(d.name, d.operator, 'Rechenzentrum'), '; ') AS names,
                   string_agg(DISTINCT d.operator, '; ') AS operators,
                   string_agg(DISTINCT d.source, ', ') AS sources,
                   count(*) AS n_entries
            FROM hits h JOIN core_bldg.data_center d ON d.id = h.id
            GROUP BY h.uuid;
            ALTER TABLE core_bldg.data_center_building ADD PRIMARY KEY (uuid);
            COMMENT ON TABLE core_bldg.data_center_building IS
              'Gebäude mit mindestens einem Rechenzentrums-Eintrag (OSM oder PeeringDB), aus core_bldg.data_center.';""")
        cur.execute("SELECT count(*), count(bldg_uuid) FROM core_bldg.data_center")
        n, m = cur.fetchone()
        cur.execute("SELECT count(*) FROM core_bldg.data_center_building")
        print(f"Rechenzentren: {n} Einträge, {m} einem Gebäude zugeordnet, {cur.fetchone()[0]} Gebäude")
    con.commit()


def dsn_from_env_file() -> str:
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("DATABASE_URL nicht in .env gefunden, bitte --dsn angeben")


STEPS = {"pv_potenzial": load_pv_potential, "pv_bestand": load_pv_installations,
         "eigentum": load_ownership, "rechenzentren": load_data_centers}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    ap.add_argument("--nur", help="Kommagetrennt: " + ",".join(STEPS))
    a = ap.parse_args()
    steps = a.nur.split(",") if a.nur else list(STEPS)
    with psycopg.connect(a.dsn or dsn_from_env_file()) as con:
        for s in steps:
            t0 = time.perf_counter()
            print(f"== {s}")
            STEPS[s](con)
            print(f"   {time.perf_counter() - t0:.0f} s")


if __name__ == "__main__":
    main()
