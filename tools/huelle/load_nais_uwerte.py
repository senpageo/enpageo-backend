"""Typische U-Werte der Gebäudehülle je Gebäude aus der NaiS-DB (KIT-IAI, MIT-Lizenz) in die Datenbank laden.

Quellen (aufbereitet im Projekt citygml2msl, data/nais und data/zuordnung):
  iwu_nwg_u_werte.csv     IWU-NWG (Nichtwohngebäude): flächengewichteter mittlerer U-Wert je Hauptfunktion, Bauteil
                          und Baujahresklasse (bis 1978, 1979-2009, ab 2010) mit Standardfehler des Mittelwerts und Fallzahl
  tabula_de_u_werte.csv   TABULA DE (Wohngebäude): Typgebäude im Ist-Zustand je Größenklasse und Baualtersklasse
  gfk_zuordnung.csv       ALKIS-Gebäudefunktion -> IWU-NWG-Gruppe bzw. Wohntyp

Tabellen:
  core_bldg.envelope_u_nwg, core_bldg.envelope_u_tabula, core_bldg.envelope_gfk_group  (Quelldaten)
  core_bldg.envelope_building   je beheiztem Gebäude: Quelle, Typ, U-Werte Wand/Dach/Boden/Fenster (+ Standardfehler NWG)

Der Sanierungsstand einzelner Gebäude ist nicht bekannt: die Werte beschreiben den typischen Zustand des Bestands
(TABULA: unsaniertes Typgebäude; IWU-NWG: Mittel der erhobenen Gebäude, teils saniert). Die TABULA-Sanierungsvarianten
sind in der NaiS-DB nicht enthalten.

Aufruf: python load_nais_uwerte.py [--src C:\\...\\citygml2msl\\data] [--dsn ...]
"""
import argparse
import csv
import io
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kaeltenetz"))
from load_strassennetz import dsn_from_env_file  # noqa: E402

DEFAULT_SRC = Path.home() / ".segberss" / "citygml2msl" / "data"


def read_csv(path: Path) -> list[dict]:
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines)), delimiter=";"))


def num(v):
    return float(v) if v not in (None, "") else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--dsn")
    a = ap.parse_args()
    nwg = read_csv(a.src / "nais" / "iwu_nwg_u_werte.csv")
    tab = read_csv(a.src / "nais" / "tabula_de_u_werte.csv")
    gfk = read_csv(a.src / "zuordnung" / "gfk_zuordnung.csv")

    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.envelope_building;
            DROP TABLE IF EXISTS core_bldg.envelope_u_nwg;
            DROP TABLE IF EXISTS core_bldg.envelope_u_tabula;
            DROP TABLE IF EXISTS core_bldg.envelope_gfk_group;
            CREATE TABLE core_bldg.envelope_u_nwg (gruppe text, hauptfunktion text, bauteil text, year_from int,
                year_to int, u_mean numeric, u_se numeric, n int, PRIMARY KEY (gruppe, bauteil, year_from));
            CREATE TABLE core_bldg.envelope_u_tabula (size_class text, year_class text, year_from int, year_to int,
                ref_area_m2 numeric, u_wall numeric, u_roof numeric, u_floor numeric, u_window numeric, code text,
                PRIMARY KEY (size_class, year_from));
            CREATE TABLE core_bldg.envelope_gfk_group (gfk int PRIMARY KEY, heated boolean, gruppe text,
                nwg_hauptfunktion text, wohntyp text, remark text);""")
        cur.executemany("INSERT INTO core_bldg.envelope_u_nwg VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        [(r["Gruppe"], r["Hauptfunktion"], r["Bauteil"], int(r["Baujahr_von"]), int(r["Baujahr_bis"]),
                          num(r["U_mittel"]), num(r["U_se"]), int(r["n"])) for r in nwg])
        cur.executemany("INSERT INTO core_bldg.envelope_u_tabula VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        [(r["Groesse"], r["Baualtersklasse"], int(r["Baujahr_von"]), int(r["Baujahr_bis"]),
                          num(r["Bezugsflaeche_m2"]), num(r["U_wand"]), num(r["U_dach"]), num(r["U_boden"]),
                          num(r["U_fenster"]), r["Code"]) for r in tab])
        cur.executemany("INSERT INTO core_bldg.envelope_gfk_group VALUES (%s,%s,%s,%s,%s,%s)",
                        [(int(r["GFK"]), r["beheizt"] == "1", r["Gruppe"] or None, r["IWU_NWG_Hauptfunktion"] or None,
                          r["Wohntyp"] or None, r["Bemerkung"] or None) for r in gfk])
        # per building: residential -> TABULA (EFH = SFH, MFH = MFH, GMH = AB bis 1978, danach MFH), otherwise IWU-NWG group (GESAMT as
        # fallback); year of construction from the KWW table (as the cooling demand); unknown year -> NULL
        cur.execute("""
            CREATE TABLE core_bldg.envelope_building AS
            WITH b AS (
                SELECT h.uuid::text AS uuid, h.gfk, h.baujahr, g.gruppe, g.wohntyp
                FROM core_bldg.kww_heat_demand_building h
                JOIN core_bldg.kww_gfk_mapping k ON k.gfk = h.gfk AND k.heated
                LEFT JOIN core_bldg.envelope_gfk_group g ON g.gfk = h.gfk
                WHERE h.scenario = '2025'),
            res AS (
                SELECT b.uuid, b.gfk, b.baujahr, 'TABULA DE (Typgebäude Ist-Zustand)' AS source,
                       t.size_class AS envelope_type, t.year_class,
                       t.u_wall, NULL::numeric AS u_wall_se, t.u_roof, NULL::numeric AS u_roof_se,
                       t.u_floor, NULL::numeric AS u_floor_se, t.u_window, NULL::numeric AS u_window_se
                FROM b JOIN core_bldg.envelope_u_tabula t
                  ON t.size_class = CASE WHEN b.wohntyp = 'EFH' THEN 'SFH'
                                         WHEN b.wohntyp = 'GMH' AND b.baujahr <= 1978 THEN 'AB' ELSE 'MFH' END
                 AND b.baujahr BETWEEN t.year_from AND t.year_to
                WHERE b.gruppe = 'WOHN'),
            nwg AS (
                SELECT b.uuid, b.gfk, b.baujahr, 'IWU-NWG (Mittel der erhobenen Gebäude)' AS source,
                       coalesce(w.gruppe, 'GESAMT') AS envelope_type,
                       w.year_from || '–' || CASE WHEN w.year_to >= 9999 THEN '' ELSE w.year_to::text END AS year_class,
                       w.u_mean AS u_wall, w.u_se AS u_wall_se, r.u_mean AS u_roof, r.u_se AS u_roof_se,
                       f.u_mean AS u_floor, f.u_se AS u_floor_se, fe.u_mean AS u_window, fe.u_se AS u_window_se
                FROM b
                CROSS JOIN LATERAL (SELECT CASE WHEN EXISTS (SELECT 1 FROM core_bldg.envelope_u_nwg x WHERE x.gruppe = b.gruppe)
                                                THEN b.gruppe ELSE 'GESAMT' END AS g) gg
                JOIN core_bldg.envelope_u_nwg w ON w.gruppe = gg.g AND w.bauteil = 'wand' AND b.baujahr BETWEEN w.year_from AND w.year_to
                JOIN core_bldg.envelope_u_nwg r ON r.gruppe = gg.g AND r.bauteil = 'dach' AND r.year_from = w.year_from
                JOIN core_bldg.envelope_u_nwg f ON f.gruppe = gg.g AND f.bauteil = 'boden' AND f.year_from = w.year_from
                JOIN core_bldg.envelope_u_nwg fe ON fe.gruppe = gg.g AND fe.bauteil = 'fenster' AND fe.year_from = w.year_from
                WHERE b.gruppe IS DISTINCT FROM 'WOHN')
            SELECT *, round((u_wall + u_roof + u_floor + u_window) / 4, 2) AS u_mean FROM res
            UNION ALL
            SELECT *, round((u_wall + u_roof + u_floor + u_window) / 4, 2) FROM nwg;
            ALTER TABLE core_bldg.envelope_building ADD PRIMARY KEY (uuid);
            COMMENT ON TABLE core_bldg.envelope_building IS
              'Typische U-Werte der Gebäudehülle je beheiztem Gebäude aus der NaiS-DB (KIT-IAI): Wohnen TABULA DE Typgebäude '
              'Ist-Zustand nach Größenklasse und Baualter, Nichtwohnen IWU-NWG-Mittelwerte je Hauptfunktion und Baujahresklasse '
              '(u_*_se = Standardfehler des Mittelwerts). u_mean = ungewichtetes Mittel der vier Bauteile. Sanierungsstand je '
              'Gebäude unbekannt.';""")
        cur.execute("""SELECT source, count(*), round(avg(u_mean), 2) FROM core_bldg.envelope_building GROUP BY 1""")
        for r in cur.fetchall():
            print(r)
        cur.execute("""SELECT count(*) FROM core_bldg.kww_heat_demand_building h
                       JOIN core_bldg.kww_gfk_mapping k ON k.gfk = h.gfk AND k.heated WHERE h.scenario = '2025'""")
        print("beheizt gesamt:", cur.fetchone()[0])
        con.commit()


if __name__ == "__main__":
    main()
