"""Ist-Kälteverbrauch und Kältestrom aus den erkannten Kälteanlagen (Rückkühler, Kühltürme) – mit Unsicherheitsband.

Rechenweg je Anlage bzw. Gebäude (Barth et al. 2023, IBPSA, doi 10.26868/25222708.2023.1259; Barth, Dissertation KIT):
    Nennkälteleistung  P   = capacity_kw der Anlagenliste (KIT 2026: Monte-Carlo-Mittel, Band ±1 σ = capacity_sd_kw);
                             ohne Leistungsspalte: Grundfläche der Anlage × spez. Leistung je Typ [kW/m²]
    wirksame Leistung      = P / Überdimensionierungsfaktor f   (installierte Leistung wird oft nicht ausgelastet)
    Kälte          E_th    = wirksame Leistung × Volllaststunden VLH (je Nutzungsgruppe aus der ALKIS-Gebäudefunktion)
    Strom          E_el    = E_th / EER (Jahres-Leistungszahl des Gesamtsystems je Anlagentyp)
Band:  min = P_min / f_max × VLH_min,   max = P_max / f_min × VLH_max;   Strom min = E_th,min / EER_max, max = E_th,max / EER_min

Zuordnung Anlage -> Gebäude: größte Überdeckung mit einem ALKIS-Umring, sonst nächstes Gebäude <= 15 m.
Steht die Anlage auf einem unbeheizten Gebäude (Parkhaus, Garage, Umformer …), wird das größte beheizte Gebäude
<= 75 m als versorgtes Gebäude angenommen (Kennzeichen 'umgehaengt'). Sehr hohe Leistung je m² BGF wird markiert
('evtl_mehrere_gebaeude'), weil eine Anlage oft mehrere Gebäude versorgt. Beides ist später gegen den
Kältebedarf aus der BGF zu prüfen.

Parametertabellen werden nur angelegt bzw. ergänzt, nie überschrieben – Werte dürfen von Hand angepasst werden.
Aufruf (nach jedem Update der Anlagenliste core_building."Chillers"): python kaelte_ist_schaetzung.py [--dsn ...]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql

# class, Bezeichnung, Bauart, kW/m² min/mittel/max, EER min/mittel/max, Quelle
CHILLER_TYPES = [
    ("ACC_Large", "Luftgekühlter Rückkühler / Verflüssiger", "luftgekuehlt", 20, 30, 40, 2.1, 2.7, 3.3,
     "Leistung: Annahme; typische luftgekühlte Kaltwassersätze ca. 25–35 kW je m² Grundfläche, Barth et al. 2023 "
     "(53,3 kW je Lüfter) bei 1,5–2,5 m² je Lüfter; EER: Barth Diss. luftgekühlte Systeme 2,7 ± 0,6"),
    ("ACC_1_Fan", "Luftgekühlter Rückkühler, 1 Lüfter", "luftgekuehlt", 10, 36, 64, 2.1, 2.7, 3.3,
     "Leistung nur ohne Anlagenliste: Median/10-/90-%-Wert der KIT-Liste 2026; EER wie luftgekühlt"),
    ("ACC_2_Fans", "Luftgekühlter Rückkühler, 2 Lüfter", "luftgekuehlt", 30, 73, 106, 2.1, 2.7, 3.3,
     "Leistung nur ohne Anlagenliste: Median/90-%-Wert der KIT-Liste 2026; EER wie luftgekühlt"),
    ("Hybrid_supposed", "Hybridkühler (vermutet)", "hybrid", 20, 30, 40, 2.1, 2.7, 3.3,
     "wie luftgekühlt (Barth gruppiert hybride mit luftgekühlten Verflüssigern)"),
    ("CT_Base_induced", "Kühlturm, induzierter Zug (Grundform)", "kuehlturm", 75, 89, 107, 3.3, 3.9, 4.5,
     "Leistung: Barth et al. 2023 Q = 107,4·A, in Manhattan 21 % überschätzt -> Mittel 107,4/1,21; "
     "EER: Barth Diss. wassergekühlte Systeme 3,9 ± 0,6. In Deutschland meist Prozesskälte"),
    ("CT_Fan_induced", "Kühlturm, induzierter Zug (Ventilator oben)", "kuehlturm", 75, 89, 107, 3.3, 3.9, 4.5,
     "wie CT_Base_induced"),
    ("CT_forced", "Kühlturm, Druckbelüftung", "kuehlturm", 74, 88, 106, 3.3, 3.9, 4.5,
     "Leistung: Barth et al. 2023 Q = 106,3·A, Korrektur wie induzierter Zug; EER wie wassergekühlt"),
    ("CT_forced_difficult", "Kühlturm, Druckbelüftung (unsicher erkannt)", "kuehlturm", 74, 88, 106, 3.3, 3.9, 4.5,
     "wie CT_forced"),
]

# Nutzungsgruppe, Bezeichnung, VLH min/mittel/max [h/a], Überdimensionierung f min/mittel/max, Quelle
COOLING_GROUPS = [
    ("wohnen", "Wohnen", 100, 250, 500, 1.0, 1.3, 2.0,
     "Annahme; Barth (NYC) 507–954 h für wärmeres Klima, Berlin deutlich niedriger"),
    ("buero_verwaltung", "Büro, Verwaltung", 300, 600, 1000, 1.0, 1.3, 2.0,
     "Richtwert 300 h (ib-aton, Klimatisierungsbedarf) bis ~600 h; Barth (NYC) 540–1160 h"),
    ("handel", "Handel", 500, 900, 1500, 1.0, 1.3, 2.0, "Annahme; Barth (NYC) Retail 720–1751 h"),
    ("hotel_gastro_heime", "Hotel, Gastronomie, Heime", 400, 900, 1600, 1.0, 1.3, 2.0,
     "Annahme; Barth (NYC) Hotel 2918–3108 h bei ganzjährigem Betrieb in wärmerem Klima"),
    ("krankenhaus_pflege", "Krankenhaus, Pflege", 800, 1400, 2400, 1.0, 1.3, 2.0,
     "Barth (NYC) Hospital 1217–2440 h; Praxisrichtwert ~800 h"),
    ("bildung_schule", "Schule, Kita", 100, 250, 500, 1.0, 1.3, 2.0,
     "Annahme (Sommerferien); Barth (NYC) School 260–861 h"),
    ("hochschule_forschung", "Hochschule, Forschung", 400, 800, 1500, 1.0, 1.3, 2.0,
     "Barth (NYC) College/University 658–1208 h; Labore höher"),
    ("kultur_versammlung", "Kultur, Versammlung, Religion", 300, 600, 1200, 1.0, 1.3, 2.0,
     "Annahme (Museen/Archive mit Klimatisierung am oberen Rand)"),
    ("sport", "Sport", 100, 250, 600, 1.0, 1.3, 2.0, "Annahme"),
    ("schwimmbad", "Schwimmbad", 100, 300, 800, 1.0, 1.3, 2.0, "Annahme (Entfeuchtung)"),
    ("produktion_gewerbe", "Produktion, Gewerbe", 800, 2000, 4000, 1.0, 1.3, 2.0,
     "Annahme; Prozesskälte (Barth: Kühltürme in Deutschland meist Prozess), Maschinensaal ~1700 h (ib-aton)"),
    ("lager", "Lager", 200, 500, 1000, 1.0, 1.3, 2.0, "Annahme"),
    ("kuehlhaus", "Kühlhaus", 3000, 4500, 6000, 1.0, 1.2, 1.5, "Annahme (ganzjährige Kühlung)"),
    ("rechenzentrum", "Rechenzentrum", 1750, 3780, 4900, 1.0, 1.2, 1.5,
     "Barth Diss.: Lastfaktor 0,6 × (1 − 0,28 freie Kühlung) × 8760 h × (1 − 0,0025) = 3780 h; "
     "Spanne Lastfaktor 0,4–0,7, freie Kühlung 0,2–0,5"),
    ("technik_verkehr_sonstige", "Technik, Verkehr, Sonstige", 300, 700, 1500, 1.0, 1.3, 2.0,
     "Barth (NYC) Public/Other 544–736 h, Spanne erweitert"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS core_bldg.chiller_type_param (
    class text PRIMARY KEY, label text, kind text,
    kw_per_m2_min numeric, kw_per_m2_mean numeric, kw_per_m2_max numeric,
    eer_min numeric, eer_mean numeric, eer_max numeric, source text);
COMMENT ON TABLE core_bldg.chiller_type_param IS
  'Kälteanlagentypen der Luftbilderkennung: spezifische Nennkälteleistung je m² Anlagengrundfläche (bis die '
  'KIT-Leistungen vorliegen) und Jahres-Leistungszahl EER des Gesamtsystems, je min/mittel/max. Von Hand anpassbar.';
CREATE TABLE IF NOT EXISTS core_bldg.cooling_usage_group (
    cooling_group text PRIMARY KEY, label text,
    vlh_min numeric, vlh_mean numeric, vlh_max numeric,
    oversize_min numeric, oversize_mean numeric, oversize_max numeric, source text);
COMMENT ON TABLE core_bldg.cooling_usage_group IS
  'Volllaststunden Kälte (bezogen auf die benötigte Leistung) und Überdimensionierungsfaktor (installiert / benötigt) '
  'je Nutzungsgruppe, min/mittel/max. Überwiegend Annahmen, gegen Kältebedarf aus der BGF zu validieren.';
CREATE TABLE IF NOT EXISTS core_bldg.cooling_gfk_group (
    gfk integer PRIMARY KEY, cooling_group text REFERENCES core_bldg.cooling_usage_group);
COMMENT ON TABLE core_bldg.cooling_gfk_group IS
  'ALKIS-Gebäudefunktion -> Nutzungsgruppe für die Volllaststunden Kälte. Rechenzentren (OSM/PeeringDB) überschreiben.';
"""

GFK_GROUP_SQL = """
INSERT INTO core_bldg.cooling_gfk_group (gfk, cooling_group)
SELECT k.gfk, CASE
    WHEN k.gfk = 2141 THEN 'kuehlhaus'
    WHEN k.residential_split OR k.kww_type IN ('ezfh', 'mfh') THEN 'wohnen'
    WHEN k.kww_type = 'buerogebaeude' THEN 'buero_verwaltung'
    WHEN k.kww_type = 'handel' THEN 'handel'
    WHEN k.kww_type = 'beherbergung_gaststaetten' THEN 'hotel_gastro_heime'
    WHEN k.kww_type = 'krankenhaeuser' THEN 'krankenhaus_pflege'
    WHEN k.kww_type = 'bildungseinrichtungen' AND k.gfk IN (2160, 3020, 3023, 3024) THEN 'hochschule_forschung'
    WHEN k.kww_type = 'bildungseinrichtungen' THEN 'bildung_schule'
    WHEN k.kww_type = 'kultureinrichtungen' THEN 'kultur_versammlung'
    WHEN k.kww_type = 'sporteinrichtungen' THEN 'sport'
    WHEN k.kww_type = 'schwimmbaeder' THEN 'schwimmbad'
    WHEN k.kww_type = 'industrie' THEN 'produktion_gewerbe'
    WHEN k.kww_type = 'lagergebaeude' THEN 'lager'
    ELSE 'technik_verkehr_sonstige' END
FROM core_bldg.kww_gfk_mapping k
ON CONFLICT (gfk) DO NOTHING;
"""

ESTIMATE_SQL = """
DROP TABLE IF EXISTS core_bldg.cooling_estimate_building;
DROP TABLE IF EXISTS core_bldg.chiller_assignment;

-- 1. Anlage -> Trägergebäude (größte Überdeckung, sonst nächstes <= 15 m)
CREATE TEMP TABLE ch AS
SELECT c.objectid, c.class, c.confidence, c.shape_area, c.geom, {capacity_col} AS capacity_kw_given,
       {capacity_sd_col} AS capacity_sd_given
FROM core_building."Chillers" c;

CREATE TEMP TABLE host AS
SELECT DISTINCT ON (ch.objectid) ch.objectid, b.uuid, b.gfk, 'ueberdeckung'::text AS match_method
FROM ch JOIN emc.building b ON ST_Intersects(b.geom, ch.geom)
WHERE b.gfk IS NOT NULL
ORDER BY ch.objectid, ST_Area(ST_Intersection(ST_MakeValid(b.geom), ST_MakeValid(ch.geom))) DESC;

INSERT INTO host
SELECT ch.objectid, nb.uuid, nb.gfk, 'naechstes_' || round(nb.d)::int || 'm'
FROM ch CROSS JOIN LATERAL (
    SELECT b.uuid, b.gfk, ST_Distance(b.geom, ch.geom) AS d FROM emc.building b
    WHERE b.gfk IS NOT NULL AND ST_DWithin(b.geom, ch.geom, 15)
    ORDER BY b.geom <-> ch.geom LIMIT 1) nb
WHERE ch.objectid NOT IN (SELECT objectid FROM host);

-- 2. unbeheizter Träger -> größtes beheiztes Gebäude <= 75 m als versorgtes Gebäude
CREATE TEMP TABLE supplied AS
SELECT h.objectid,
       CASE WHEN h.gfk BETWEEN {supply_from} AND {supply_to} THEN NULL
            WHEN coalesce(k.heated, true) THEN h.uuid ELSE cand.uuid END AS uuid,
       CASE WHEN h.gfk BETWEEN {supply_from} AND {supply_to} THEN 'energieversorgung_ausgeschlossen'
            WHEN coalesce(k.heated, true) THEN NULL
            WHEN cand.uuid IS NULL THEN 'traeger_unbeheizt_kein_ersatz'
            ELSE 'umgehaengt_von_unbeheizt' END AS flag
FROM host h
LEFT JOIN core_bldg.kww_gfk_mapping k ON k.gfk = h.gfk
LEFT JOIN LATERAL (
    SELECT b.uuid FROM emc.building b
    JOIN core_bldg.kww_gfk_mapping k2 ON k2.gfk = b.gfk AND k2.heated
    LEFT JOIN core_bldg.kww_heat_demand_building kh ON kh.uuid = b.uuid AND kh.scenario = '2025'
    WHERE NOT coalesce(k.heated, true) AND ST_DWithin(b.geom, (SELECT geom FROM ch WHERE ch.objectid = h.objectid), 75)
    ORDER BY kh.bgf_m2 DESC NULLS LAST LIMIT 1) cand ON true;

CREATE TABLE core_bldg.chiller_assignment AS
SELECT ch.objectid, ch.class, round(ch.confidence::numeric, 1) AS confidence, round(ch.shape_area::numeric, 1) AS area_m2,
       h.uuid AS host_uuid, h.gfk AS host_gfk, h.match_method,
       CASE WHEN s.flag = 'energieversorgung_ausgeschlossen' THEN NULL ELSE coalesce(s.uuid, h.uuid) END AS supplied_uuid,
       b2.gfk AS supplied_gfk, s.flag,
       CASE WHEN ch.capacity_kw_given IS NOT NULL THEN 'Anlagenliste' ELSE 'Flaechenregression' END AS capacity_source,
       -- Anlagenliste: Mittel ± 1 Standardabweichung (ohne Angabe ±20 %), sonst Fläche × kW/m² je Typ
       round(coalesce(greatest(ch.capacity_kw_given - coalesce(ch.capacity_sd_given, 0.2 * ch.capacity_kw_given), 0),
                      ch.shape_area * t.kw_per_m2_min)::numeric, 1) AS capacity_kw_min,
       round(coalesce(ch.capacity_kw_given, ch.shape_area * t.kw_per_m2_mean)::numeric, 1) AS capacity_kw_mean,
       round(coalesce(ch.capacity_kw_given + coalesce(ch.capacity_sd_given, 0.2 * ch.capacity_kw_given),
                      ch.shape_area * t.kw_per_m2_max)::numeric, 1) AS capacity_kw_max,
       t.eer_min, t.eer_mean, t.eer_max, t.kind
FROM ch
LEFT JOIN host h ON h.objectid = ch.objectid
LEFT JOIN supplied s ON s.objectid = ch.objectid
LEFT JOIN emc.building b2 ON b2.uuid = s.uuid
LEFT JOIN core_bldg.chiller_type_param t ON t.class = ch.class;
ALTER TABLE core_bldg.chiller_assignment ADD PRIMARY KEY (objectid);
CREATE INDEX ON core_bldg.chiller_assignment (supplied_uuid);
COMMENT ON TABLE core_bldg.chiller_assignment IS
  'Erkannte Kälteanlage -> Trägergebäude (host) und vermutlich versorgtes Gebäude (supplied) mit ALKIS-Funktion, '
  'geschätzter Nennkälteleistung (min/mittel/max) und EER. flag: umgehaengt_von_unbeheizt, traeger_unbeheizt_kein_ersatz.';

-- 3. Gebäude: Leistung summieren, Volllaststunden und Überdimensionierung je Nutzungsgruppe
CREATE TABLE core_bldg.cooling_estimate_building AS
WITH per_bldg AS (
    SELECT a.supplied_uuid AS uuid, count(*) AS n_units,
           string_agg(DISTINCT a.class, ', ') AS unit_classes,
           bool_or(a.kind = 'kuehlturm') AS has_cooling_tower,
           sum(a.capacity_kw_min) AS cap_min, sum(a.capacity_kw_mean) AS cap_mean, sum(a.capacity_kw_max) AS cap_max,
           -- EER leistungsgewichtet über die Anlagen des Gebäudes
           sum(a.capacity_kw_mean * a.eer_min) / nullif(sum(a.capacity_kw_mean), 0) AS eer_min,
           sum(a.capacity_kw_mean * a.eer_mean) / nullif(sum(a.capacity_kw_mean), 0) AS eer_mean,
           sum(a.capacity_kw_mean * a.eer_max) / nullif(sum(a.capacity_kw_mean), 0) AS eer_max,
           string_agg(DISTINCT a.flag, ', ') AS assignment_flags,
           string_agg(DISTINCT a.capacity_source, ', ') AS capacity_source
    FROM core_bldg.chiller_assignment a
    WHERE a.supplied_uuid IS NOT NULL
    GROUP BY a.supplied_uuid
), g AS (
    SELECT p.*, b.gfk,
           CASE WHEN dcb.uuid IS NOT NULL THEN 'rechenzentrum' ELSE coalesce(cg.cooling_group, 'technik_verkehr_sonstige') END
               AS cooling_group,
           kh.bgf_m2
    FROM per_bldg p
    JOIN emc.building b ON b.uuid = p.uuid
    LEFT JOIN core_bldg.cooling_gfk_group cg ON cg.gfk = b.gfk
    LEFT JOIN core_bldg.data_center_building dcb ON dcb.uuid = p.uuid
    LEFT JOIN core_bldg.kww_heat_demand_building kh ON kh.uuid = p.uuid AND kh.scenario = '2025'
)
SELECT g.uuid, g.gfk, g.cooling_group, u.label AS cooling_group_label, g.n_units, g.unit_classes, g.has_cooling_tower,
       round(g.cap_min, 1) AS capacity_kw_min, round(g.cap_mean, 1) AS capacity_kw_mean, round(g.cap_max, 1) AS capacity_kw_max,
       u.vlh_min, u.vlh_mean, u.vlh_max, u.oversize_min, u.oversize_mean, u.oversize_max,
       round(g.cap_min / u.oversize_max * u.vlh_min) AS cooling_kwh_min,
       round(g.cap_mean / u.oversize_mean * u.vlh_mean) AS cooling_kwh_mean,
       round(g.cap_max / u.oversize_min * u.vlh_max) AS cooling_kwh_max,
       round(g.cap_min / u.oversize_max * u.vlh_min / g.eer_max) AS electricity_kwh_min,
       round(g.cap_mean / u.oversize_mean * u.vlh_mean / g.eer_mean) AS electricity_kwh_mean,
       round(g.cap_max / u.oversize_min * u.vlh_max / g.eer_min) AS electricity_kwh_max,
       round(g.eer_mean, 2) AS eer_mean,
       g.bgf_m2,
       round(g.cap_mean * 1000 / nullif(g.bgf_m2, 0)) AS capacity_w_per_m2_bgf,
       concat_ws(', ', g.assignment_flags,
                 -- Prozess, Kühlhaus, RZ dürfen viel Leistung je m² haben, aber nicht > 1 kW/m² BGF
                 CASE WHEN g.cap_mean * 1000 / nullif(g.bgf_m2, 0) >
                           CASE WHEN g.cooling_group IN ('rechenzentrum', 'kuehlhaus', 'produktion_gewerbe') THEN 1000 ELSE 300 END
                      THEN 'evtl_mehrere_gebaeude' END,
                 CASE WHEN g.has_cooling_tower AND g.cooling_group NOT IN ('produktion_gewerbe', 'rechenzentrum', 'kuehlhaus')
                      THEN 'kuehlturm_evtl_prozesskaelte' END) AS check_flags,
       g.capacity_source
FROM g JOIN core_bldg.cooling_usage_group u ON u.cooling_group = g.cooling_group;
ALTER TABLE core_bldg.cooling_estimate_building ADD PRIMARY KEY (uuid);
COMMENT ON TABLE core_bldg.cooling_estimate_building IS
  'Ist-Kälteverbrauch und Kältestrom je Gebäude aus erkannten Kälteanlagen, mit Unsicherheitsband (min/mittel/max). '
  'Grobe Schätzung: Nennleistung aus Anlagenfläche (bzw. Anlagenliste), Überdimensionierung, Volllaststunden je '
  'Nutzungsgruppe, EER je Anlagentyp. check_flags: Zuordnung prüfen (Validierung gegen Kältebedarf aus BGF ausstehend).';
"""


def dsn_from_env_file() -> str:
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("DATABASE_URL nicht in .env gefunden, bitte --dsn angeben")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    a = ap.parse_args()
    t0 = time.perf_counter()
    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute(SCHEMA)
        cur.executemany("INSERT INTO core_bldg.chiller_type_param VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (class) DO NOTHING", CHILLER_TYPES)
        cur.executemany("INSERT INTO core_bldg.cooling_usage_group VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (cooling_group) DO NOTHING", COOLING_GROUPS)
        cur.execute(GFK_GROUP_SQL)
        # keep the hand-editable parameters, but apply the corrected defaults of this version once
        cur.execute("UPDATE core_bldg.chiller_type_param SET kw_per_m2_min = 20, kw_per_m2_mean = 30, kw_per_m2_max = 40 "
                    "WHERE class IN ('ACC_Large', 'Hybrid_supposed') AND kw_per_m2_mean = 45")
        # Use the capacity of the chiller list when present (KIT 2026: capacity_kw + capacity_sd_kw).
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'core_building' "
                    "AND table_name = 'Chillers'")
        cols = {r[0] for r in cur.fetchall()}
        col = next((c for c in ("capacity_kw", "kaelteleistung_kw") if c in cols), None)
        sd_col = "capacity_sd_kw" if col == "capacity_kw" and "capacity_sd_kw" in cols else None
        # Views on the result table (street segment load) would block the DROP: keep and recreate them.
        cur.execute("""SELECT DISTINCT v.oid::regclass::text, pg_get_viewdef(v.oid), obj_description(v.oid)
                       FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class
                       WHERE d.refobjid = to_regclass('core_bldg.cooling_estimate_building') AND v.relkind = 'v'""")
        views = cur.fetchall()
        for name, _, _ in views:
            cur.execute(f"DROP VIEW {name}")
        # Ver- und Entsorgung (Kraft-, Heiz-, Umspannwerke …): Kühltürme der Energieerzeugung, keine Gebäudekühlung
        cur.execute(ESTIMATE_SQL.format(capacity_col=f'c."{col}"' if col else "NULL::numeric",
                                        capacity_sd_col=f'c."{sd_col}"' if sd_col else "NULL::numeric",
                                        supply_from=2500, supply_to=2622))
        for name, definition, comment in views:
            cur.execute(f"CREATE VIEW {name} AS {definition}")
            if comment:
                cur.execute(sql.SQL("COMMENT ON VIEW {} IS {}").format(sql.SQL(name), sql.Literal(comment)))
        cur.execute("""SELECT count(*), count(host_uuid), count(*) FILTER (WHERE flag IS NOT NULL)
                       FROM core_bldg.chiller_assignment""")
        n, assigned, flagged = cur.fetchone()
        cur.execute("""SELECT count(*), round(sum(capacity_kw_mean) / 1000), round(sum(cooling_kwh_min) / 1e6),
                              round(sum(cooling_kwh_mean) / 1e6), round(sum(cooling_kwh_max) / 1e6),
                              round(sum(electricity_kwh_mean) / 1e6),
                              count(*) FILTER (WHERE check_flags <> '')
                       FROM core_bldg.cooling_estimate_building""")
        nb, mw, gmin, gmean, gmax, el, chk = cur.fetchone()
        con.commit()
    print(f"Anlagen: {n}, einem Gebäude zugeordnet: {assigned}, umgehängt/markiert: {flagged}")
    print(f"Gebäude mit Anlage: {nb}, Nennleistung ~{mw} MW, Kälte {gmean} GWh/a (Band {gmin}–{gmax}), "
          f"Strom {el} GWh/a, zu prüfen: {chk}  ({time.perf_counter() - t0:.0f} s)")
    if col is None:
        print("Hinweis: Anlagenliste ohne Leistungsspalte -> Nennleistung aus Anlagenfläche (Barth-Regressionen).")


if __name__ == "__main__":
    main()
