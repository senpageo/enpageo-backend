"""Wärmebelastung aus dem Klimamodell Berlin 2022 (Umweltatlas, gdi.berlin.de, Datenlizenz Deutschland Zero 2.0) laden.

Je Block(teil)fläche (Schlüssel schl5) für eine austauscharme sommerliche Strahlungswetterlage:
  core_base.climate_area      kind = siedlung | verkehr | gruen;
                              pet14h  Physiologisch Äquivalente Temperatur um 14 Uhr [°C] (Wärmebelastung am Tag)
                              t2m04h  Lufttemperatur in 2 m Höhe um 4 Uhr [°C] (nächtliche Überwärmung)
                              rating  Bioklimatische Gesamtbewertung Tag+Nacht (Siedlung, Verkehr) bzw.
                                      Aufenthaltsqualität am Tag (Grün- und Freiflächen)
  core_base.climate_vulnerable  Stadtgebiete mit sehr ungünstiger thermischer Belastung (Tag+Nacht) und stadtklimatisch
                              besonders vulnerable Stadtgebiete (Demografie, Nutzung, Grünflächenunterversorgung)
  core_bldg.building_climate  je Gebäude: Block(teil)fläche der Siedlungs- bzw. Verkehrsfläche unter dem Gebäude
                              (Punkt auf der Fläche, sonst nächste Siedlungsfläche bis 50 m) mit ihren Werten

Aufruf: python load_klima.py [--dsn ...]
"""
import argparse
import json
import sys
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kaeltenetz"))
from load_strassennetz import dsn_from_env_file  # noqa: E402

WFS = "https://gdi.berlin.de/services/wfs/{svc}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES={svc}:{layer}" \
      "&SRSNAME=EPSG:25833&OUTPUTFORMAT=application/json&COUNT={count}&STARTINDEX={start}&SORTBY=schl5"
UA = {"User-Agent": "enpageo-coolingmap/1.0"}
A, B = "ua_klimaanalyse_2022", "ua_klimabewertung_2022"

# kind -> layers per value (service, layer, attribute)
LAYERS = {
    "siedlung": {"pet14h": (A, "pa_ua_pet_siedlg_2022", "pet14h"), "t2m04h": (A, "fa_ua_lufttemp_siedlg_t2m_04h_2022", "t2m04h"),
                 "rating": (B, "bj_ua_phk_biokl_siedl_2022", "phk_gesamt")},
    "verkehr": {"pet14h": (A, "pb_ua_pet_str_2022", "pet14h"), "t2m04h": (A, "fb_ua_lufttemp_str_t2m_04h_2022", "t2m04h"),
                "rating": (B, "bi_ua_phk_biokl_str_2022", None)},
    "gruen": {"pet14h": (A, "pc_ua_pet_grfrei_2022", "pet14h"), "t2m04h": (A, "fc_ua_lufttemp_grfrei_t2m_04h_2022", "t2m04h"),
              "rating": (B, "ca_ua_phk_aufenth_grfrei_tag_2022", "pet14h_tag_klar")},
}
VULNERABLE = [(B, "ea_ua_phk_vul_geb_therm_belast_2022", "sehr ungünstige thermische Belastung Tag und Nacht"),
              (B, "eb_ua_phk_vul_demog_2022", "stadtklimatisch besonders vulnerabel")]


def fetch(svc: str, layer: str) -> list[dict]:
    out, start = [], 0
    while True:
        r = httpx.get(WFS.format(svc=svc, layer=layer, count=10000, start=start), headers=UA, timeout=600)
        r.raise_for_status()
        try:
            text = r.content.decode("utf-8")
        except UnicodeDecodeError:  # some layers come in Latin-1
            text = r.content.decode("latin-1")
        feats = json.loads(text)["features"]
        out += feats
        if len(feats) < 10000:
            return out
        start += 10000


def first_text_value(props: dict, attr: str | None):
    if attr:
        return props.get(attr)
    vals = [v for k, v in props.items() if isinstance(v, str) and k not in ("schl5", "woz_name", "grz_name", "typklar")]
    return vals[0] if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    a = ap.parse_args()
    rows: dict[tuple[str, str], dict] = {}
    for kind, layers in LAYERS.items():
        for key, (svc, layer, attr) in layers.items():
            feats = fetch(svc, layer)
            print(f"{kind}/{key}: {len(feats)} Flächen ({layer})")
            for f in feats:
                p = f["properties"]
                r = rows.setdefault((kind, p["schl5"]), {"geom": f["geometry"]})
                r[key] = first_text_value(p, attr) if key == "rating" else p.get(attr)
    vul = []
    for svc, layer, label in VULNERABLE:
        feats = fetch(svc, layer)
        print(f"vulnerabel: {len(feats)} Flächen ({layer})")
        for f in feats:
            p = f["properties"]
            detail = "; ".join(f"{k}: {v}" for k, v in p.items() if v not in (None, "", "0") and k not in ("schl5",))
            vul.append((layer, label, p.get("schl5"), detail, json.dumps(f["geometry"])))

    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.building_climate;
            DROP TABLE IF EXISTS core_base.climate_area;
            DROP TABLE IF EXISTS core_base.climate_vulnerable;
            CREATE TABLE core_base.climate_area (kind text, schl5 text, pet14h numeric, t2m04h numeric, rating text,
                geom geometry(MultiPolygon, 25833), PRIMARY KEY (kind, schl5));
            CREATE TABLE core_base.climate_vulnerable (id serial PRIMARY KEY, layer text, label text, schl5 text,
                detail text, geom geometry(MultiPolygon, 25833));""")
        cur.executemany(
            "INSERT INTO core_base.climate_area VALUES (%s, %s, %s, %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833)))",
            [(k, s, r.get("pet14h"), r.get("t2m04h"), r.get("rating"), json.dumps(r["geom"])) for (k, s), r in rows.items()])
        cur.executemany(
            "INSERT INTO core_base.climate_vulnerable (layer, label, schl5, detail, geom) "
            "VALUES (%s, %s, %s, %s, ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 25833)))", vul)
        cur.execute("""
            CREATE INDEX ON core_base.climate_area USING gist (geom);
            CREATE INDEX ON core_base.climate_vulnerable USING gist (geom);
            ANALYZE core_base.climate_area;
            -- buildings: the settlement block (or street area) under the building; else the nearest settlement block
            CREATE TABLE core_bldg.building_climate AS
            SELECT b.uuid::text AS uuid, c.kind, c.schl5, c.pet14h, c.t2m04h, c.rating,
                   EXISTS (SELECT 1 FROM core_base.climate_vulnerable v
                           WHERE v.layer = 'ea_ua_phk_vul_geb_therm_belast_2022'
                             AND ST_Intersects(v.geom, ST_PointOnSurface(b.geom))) AS very_unfavourable,
                   EXISTS (SELECT 1 FROM core_base.climate_vulnerable v
                           WHERE v.layer = 'eb_ua_phk_vul_demog_2022'
                             AND ST_Intersects(v.geom, ST_PointOnSurface(b.geom))) AS vulnerable_area
            FROM emc.building b
            CROSS JOIN LATERAL (
                SELECT c.* FROM core_base.climate_area c
                WHERE c.kind IN ('siedlung', 'verkehr') AND ST_DWithin(c.geom, ST_PointOnSurface(b.geom), 50)
                ORDER BY ST_Distance(c.geom, ST_PointOnSurface(b.geom)), c.kind = 'verkehr' LIMIT 1) c
            WHERE b.displayed;
            ALTER TABLE core_bldg.building_climate ADD PRIMARY KEY (uuid);
            COMMENT ON TABLE core_base.climate_area IS 'Klimamodell Berlin 2022 (Umweltatlas, gdi.berlin.de, dl-de/zero-2-0) '
              'je Block(teil)fläche: PET 14 Uhr [°C], Lufttemperatur 4 Uhr [°C], Bewertung; austauscharme sommerliche '
              'Strahlungswetterlage, Ist-Zustand.';
            COMMENT ON TABLE core_bldg.building_climate IS 'Je angezeigtem Gebäude die Werte der Siedlungs-/Verkehrsfläche '
              'darunter (core_base.climate_area) und ob es in einem sehr ungünstig belasteten bzw. vulnerablen Stadtgebiet liegt.';""")
        cur.execute("SELECT kind, count(*), round(avg(pet14h), 1), round(avg(t2m04h), 1) FROM core_base.climate_area GROUP BY 1")
        print(cur.fetchall())
        cur.execute("SELECT count(*), count(*) FILTER (WHERE very_unfavourable), count(*) FILTER (WHERE vulnerable_area) "
                    "FROM core_bldg.building_climate")
        print("Gebäude (gesamt, sehr ungünstig, vulnerabel):", cur.fetchone())
        con.commit()


if __name__ == "__main__":
    main()
