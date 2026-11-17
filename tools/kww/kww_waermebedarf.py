"""Schnellrechnung Heizwärme + Warmwasser und Prozesswärme-Relevanz nach KWW-Technikkatalog.

Rechenweg je Gebäude (Berlin, emc.building):
    Nutzung        = ALKIS-Gebäudefunktion (gfk) -> core_bldg.kww_gfk_mapping
                     Wohngebäude (1000/1010/...): EZFH bei EnergyMap-Typ EFH/RH, MFH bei MFH/GMH/HH,
                     ohne EnergyMap-Typ nach Geschossen (<= 2 -> EZFH)
    BGF [m²]       = Grundfläche ALKIS-Umring * oberirdische Geschosse (aog);
                     fehlt aog: Geschosszahl SimStadt (emc.param_building), dann Höhe / 3 m, sonst 1
    Kennwert       = core_bldg.kww_spec_demand für Typ, Baualtersklasse (Baujahr aus emp.emblive)
                     und Szenario; ohne Baujahr oder bei 'k. A.' der Wert 'Alle Klassen'
    Zwischenjahre  = geometrisch zwischen 2025 und 2045: w(J) = w2025 * (w2045/w2025)^((J-2025)/20)
    Bedarf [kWh/a] = BGF * Kennwert  (Endenergie Heizung + Warmwasser, ohne Prozesswärme)
    NGF [m²]       = BGF / Umrechnungsfaktor BGF/NGF (nur Information)
    Prozesswärme   = nur Relevanz laut Tab 56 (Ja/Nein, Datenerfassung, Voraussetzung), KEINE kWh,
                     weil der Katalog dafür keine spezifischen Werte enthält.

Beispiele:
    python kww_waermebedarf.py --gfk 3221                      # alle Hallenbäder, Szenario 2025
    python kww_waermebedarf.py --plz 12043 --szenario 2045_o45 --csv neukoelln.csv
    python kww_waermebedarf.py --jahr 2035 --pfad uba --summe   # ganz Berlin, Summen je Typ
    python kww_waermebedarf.py --tabelle-schreiben              # alle Szenarien nach core_bldg.kww_heat_demand_building

Als Modul (z. B. im Ask-the-Map-Backend):
    from kww_waermebedarf import berechne
    rows = berechne(con, szenario="2025", uuids=[...])
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SZENARIEN = ("2025", "2045_o45", "2045_uba", "2045_fortschreibung")
PFADE = {"o45": "2045_o45", "uba": "2045_uba", "fortschreibung": "2045_fortschreibung"}

# Ein Kennwert je Gebäude, Typ und Szenario. %(...)s sind psycopg-Parameter.
_SQL = """
WITH g AS (
    SELECT b.uuid, b.gfk, k.gfk_name, k.heated, k.mapping_quality, k.process_heat_function,
           coalesce(b.gf, ST_Area(b.geom)) AS footprint_m2,
           b.aog, b.height,
           CASE WHEN b.aog > 0 THEN b.aog
                WHEN pb."Storey number" > 0 THEN round(pb."Storey number")
                WHEN b.height > 0 THEN greatest(1, round(b.height / 3.0))
                ELSE 1 END AS storeys,
           CASE WHEN b.aog > 0 THEN 'aog' WHEN pb."Storey number" > 0 THEN 'simstadt'
                WHEN b.height > 0 THEN 'hoehe/3m' ELSE 'annahme_1' END AS storeys_source,
           e.year_of_construction AS baujahr,
           e.buildingtype_emb,
           CASE WHEN NOT k.residential_split THEN k.kww_type
                WHEN e.buildingtype_emb IN ('EFH', 'RH') THEN 'ezfh'
                WHEN e.buildingtype_emb IN ('MFH', 'GMH', 'HH') THEN 'mfh'
                WHEN coalesce(b.aog, 1) <= 2 THEN 'ezfh'
                ELSE 'mfh' END AS kww_type
    FROM emc.building b
    JOIN core_bldg.kww_gfk_mapping k USING (gfk)
    LEFT JOIN emp.emblive e USING (uuid)
    LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
    WHERE b.gfk IS NOT NULL {filter}
)
SELECT g.uuid, g.gfk, g.gfk_name, g.heated, g.kww_type, t.label AS kww_label, g.mapping_quality,
       round(g.footprint_m2::numeric, 1) AS footprint_m2, g.storeys::int AS storeys, g.storeys_source,
       round((g.footprint_m2 * g.storeys)::numeric, 1) AS bgf_m2,
       round((g.footprint_m2 * g.storeys / t.bgf_ngf_factor)::numeric, 1) AS ngf_m2,
       g.baujahr, s.age_class,
       round(s.spec::numeric, 2) AS kwh_per_m2_bgf_a,
       CASE WHEN g.heated THEN round((g.footprint_m2 * g.storeys * s.spec)::numeric, 0) END AS heat_dhw_kwh_a,
       p.process_heat, p.data_collection AS process_heat_data_collection, p.precondition AS process_heat_precondition
FROM g
LEFT JOIN core_bldg.kww_bldg_type t USING (kww_type)
LEFT JOIN LATERAL (
    SELECT d0.age_class,
           d0.kwh_per_m2_bgf_a
             * power(d1.kwh_per_m2_bgf_a / d0.kwh_per_m2_bgf_a, %(w)s) AS spec
    FROM core_bldg.kww_spec_demand d0
    JOIN core_bldg.kww_spec_demand d1
      ON d1.kww_type = d0.kww_type AND d1.age_class = d0.age_class AND d1.scenario = %(ziel)s
    WHERE d0.kww_type = g.kww_type AND d0.scenario = '2025'
      AND d0.kwh_per_m2_bgf_a IS NOT NULL AND d1.kwh_per_m2_bgf_a IS NOT NULL
      AND (d0.age_class = 'Alle Klassen'
           OR (g.baujahr IS NOT NULL
               AND g.baujahr >= coalesce(d0.year_from, -32768)
               AND g.baujahr <= coalesce(d0.year_to, 32767)))
    ORDER BY (d0.age_class = 'Alle Klassen')
    LIMIT 1
) s ON g.heated
LEFT JOIN core_bldg.kww_process_heat p ON p.function_label = g.process_heat_function
"""


def _gewicht(szenario: str, jahr: int | None) -> tuple[str, float]:
    """Zielszenario + Exponent für die geometrische Interpolation 2025 -> 2045."""
    if szenario == "2025":
        return "2025", 0.0
    if jahr is None:
        return szenario, 1.0
    if not 2025 <= jahr <= 2045:
        raise ValueError("--jahr muss zwischen 2025 und 2045 liegen")
    return szenario, (jahr - 2025) / 20.0


def berechne(con, szenario: str = "2025", jahr: int | None = None, uuids=None, gfk=None,
             plz=None, bbox=None, nur_beheizt: bool = False) -> list[dict]:
    """Gibt je Gebäude ein dict zurück. bbox = (xmin, ymin, xmax, ymax) in EPSG:4326."""
    if szenario not in SZENARIEN:
        raise ValueError(f"Szenario {szenario!r} unbekannt, erlaubt: {SZENARIEN}")
    ziel, w = _gewicht(szenario, jahr)
    filt, params = [], {"ziel": ziel, "w": w}
    if uuids:
        filt.append("b.uuid = ANY(%(uuids)s)"); params["uuids"] = list(uuids)
    if gfk:
        filt.append("b.gfk = ANY(%(gfk)s)"); params["gfk"] = [int(x) for x in gfk]
    if plz:
        filt.append("e.postcode = ANY(%(plz)s)"); params["plz"] = [int(x) for x in plz]
    if bbox:
        filt.append("b.geom && ST_Transform(ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, 4326), 25833)")
        params.update(zip(("x0", "y0", "x1", "y1"), bbox))
    if nur_beheizt:
        filt.append("k.heated")
    sql = _SQL.format(filter="".join(" AND " + f for f in filt))
    with con.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def schreibe_tabelle(con) -> int:
    """Alle Gebäude x 4 Szenarien nach core_bldg.kww_heat_demand_building (neu aufgebaut)."""
    with con.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.kww_heat_demand_building;
            CREATE TABLE core_bldg.kww_heat_demand_building (
                uuid varchar NOT NULL, scenario text NOT NULL REFERENCES core_bldg.kww_scenario,
                gfk integer, kww_type text, mapping_quality text,
                footprint_m2 numeric, storeys integer, storeys_source text, bgf_m2 numeric, ngf_m2 numeric,
                baujahr integer, age_class text, kwh_per_m2_bgf_a numeric, heat_dhw_kwh_a numeric,
                process_heat text, process_heat_data_collection text,
                computed_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (uuid, scenario));
            COMMENT ON TABLE core_bldg.kww_heat_demand_building IS
              'Ergebnis kww_waermebedarf.py: Endenergie Heizung+WW nach KWW-Technikkatalog (BGF-Bezug, ALKIS-GFK), je Gebäude und Szenario. Prozesswärme nur als Relevanz.';
        """)
        n = 0
        for sz in SZENARIEN:
            ziel, w = _gewicht(sz, None)
            cur.execute(
                "INSERT INTO core_bldg.kww_heat_demand_building "
                "(uuid, scenario, gfk, kww_type, mapping_quality, footprint_m2, storeys, storeys_source, bgf_m2, "
                " ngf_m2, baujahr, age_class, kwh_per_m2_bgf_a, heat_dhw_kwh_a, process_heat, process_heat_data_collection) "
                f"SELECT uuid, %(sz)s, gfk, kww_type, mapping_quality, footprint_m2, storeys, storeys_source, bgf_m2, "
                f" ngf_m2, baujahr, age_class, kwh_per_m2_bgf_a, heat_dhw_kwh_a, process_heat, process_heat_data_collection "
                f"FROM ({_SQL.format(filter='')}) x",
                {"sz": sz, "ziel": ziel, "w": w})
            n += cur.rowcount
        cur.execute("CREATE INDEX ON core_bldg.kww_heat_demand_building (scenario, kww_type)")
        cur.execute("CREATE INDEX ON core_bldg.kww_heat_demand_building (gfk)")
    con.commit()
    return n


def summen(rows: list[dict]) -> list[tuple]:
    agg: dict[str, list] = {}
    for r in rows:
        key = "unbeheizt" if not r["heated"] else (r["kww_label"] or "kein KWW-Typ")
        a = agg.setdefault(key, [0, 0.0, 0.0, 0])
        a[0] += 1
        a[1] += float(r["bgf_m2"] or 0) if r["heated"] else 0
        a[2] += float(r["heat_dhw_kwh_a"] or 0)
        a[3] += r["process_heat"] in ("Ja", "Ja/Nein")
    return sorted(((k, *v) for k, v in agg.items()), key=lambda x: -x[3])


def _dsn_aus_env() -> str:
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("DATABASE_URL nicht in .env gefunden, bitte --dsn angeben")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--szenario", default="2025", choices=SZENARIEN)
    ap.add_argument("--pfad", choices=PFADE, help="mit --jahr: Pfad o45 | uba | fortschreibung")
    ap.add_argument("--jahr", type=int, help="Zwischenjahr 2025–2045 (geometrisch interpoliert)")
    ap.add_argument("--uuid", nargs="+")
    ap.add_argument("--gfk", nargs="+", type=int)
    ap.add_argument("--plz", nargs="+", type=int)
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"), help="WGS84")
    ap.add_argument("--nur-beheizt", action="store_true")
    ap.add_argument("--csv", help="Ergebnis je Gebäude als CSV (Semikolon, UTF-8)")
    ap.add_argument("--summe", action="store_true", help="nur Summen je KWW-Typ ausgeben")
    ap.add_argument("--tabelle-schreiben", action="store_true",
                    help="alle Gebäude x Szenarien nach core_bldg.kww_heat_demand_building")
    ap.add_argument("--dsn")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    szenario = a.szenario
    if a.jahr is not None:
        if a.jahr > 2025 and not a.pfad and szenario == "2025":
            ap.error("--jahr > 2025 braucht --pfad (o45 | uba | fortschreibung)")
        if a.pfad:
            szenario = PFADE[a.pfad]

    t0 = time.perf_counter()
    with psycopg.connect(a.dsn or _dsn_aus_env()) as con:
        if a.tabelle_schreiben:
            n = schreibe_tabelle(con)
            print(f"core_bldg.kww_heat_demand_building: {n} Zeilen in {time.perf_counter() - t0:.1f} s")
            return
        rows = berechne(con, szenario, a.jahr, a.uuid, a.gfk, a.plz, a.bbox, a.nur_beheizt)
    dt = time.perf_counter() - t0

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["uuid"], delimiter=";")
            w.writeheader(); w.writerows(rows)
    if a.summe or not a.csv:
        label = szenario + (f", Jahr {a.jahr}" if a.jahr else "")
        print(f"{len(rows)} Gebäude, Szenario {label}, {dt:.1f} s")
        print(f"{'KWW-Typ':42} {'Gebäude':>9} {'BGF beheizt [m²]':>17} {'Wärme [GWh/a]':>14} {'Prozessw. Ja':>12}")
        tot = [0, 0.0, 0.0, 0]
        for k, n, bgf, kwh, ph in summen(rows):
            print(f"{k:42} {n:9d} {bgf:17,.0f} {kwh / 1e6:14,.1f} {ph:12d}")
            tot = [tot[0] + n, tot[1] + bgf, tot[2] + kwh, tot[3] + ph]
        print(f"{'Summe':42} {tot[0]:9d} {tot[1]:17,.0f} {tot[2] / 1e6:14,.1f} {tot[3]:12d}")
        if not a.summe and len(rows) <= 30:
            for r in rows:
                print(f"  {r['uuid']}  gfk {r['gfk']} {r['gfk_name'][:28]:28} {r['kww_type'] or '-':14} "
                      f"BGF {r['bgf_m2'] or 0:>9} m² × {r['kwh_per_m2_bgf_a'] or '-':>7} = "
                      f"{r['heat_dhw_kwh_a'] or '-':>9} kWh/a  Prozesswärme {r['process_heat'] or '-'}")
    if a.csv:
        print(f"CSV geschrieben: {a.csv}")


if __name__ == "__main__":
    main()
