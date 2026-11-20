"""Mock-up: further measures (window replacement, retrofit) and estimated indoor overheating per building.

Everything here is a placeholder until the project's SimStadt simulations arrive; the chat tools only need the
structure. Run after kaeltebedarf_mockup.sql and kaeltebedarf_basis.sql (those drop core_bldg.demand_measure).

1. core_bldg.demand_measure: adds 'fenstertausch' and 'sanierung' (flat % on cooling demand, assumptions) and the
   column indoor_delta_k (shift of the indoor operative temperature, assumption).
2. Indoor temperatures: three free-running hourly series (no chiller, window opening on overheating) from
   citygml2msl, run 'drk_westend_grob_nutzungstest' (weather DRKK 2023, coarse one-zone model):
     wohnen      = wohnen_1875  (GFK 1000, residential, 1875)
     buero       = buero_1985   (GFK 2020, office, 1985)
     krankenhaus = haus11       (GFK 3051, health and care, 1920)
   Per building the series is shifted by a flat delta in K:
     climate (year, scenario) + measures + age class + local outdoor heat (PET 14 h of the climate model 2022),
   and core_bldg.indoor_lookup holds, for every reference, delta (-6..+8 K, step 0.1) and threshold (26..32 °C),
   the days whose maximum operative temperature exceeds the threshold and the hours above 26 °C.

Usage:  python tools/kaelte/innenraum_mockup.py [--dsn postgresql://...] [--citygml2msl PATH]
"""
import argparse
import csv
from collections import defaultdict
from pathlib import Path

import sys

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kaeltenetz"))
from load_strassennetz import dsn_from_env_file  # noqa: E402

RUN = "ausgabe/drk_westend_grob_nutzungstest/ergebnisse"
REFS = {
    "wohnen": ("stundenwerte_wohnen_1875_2023.csv", "Wohngebäude 1875, Massivbau (GFK 1000)", "1919–1948"),
    "buero": ("stundenwerte_buero_1985_2023.csv", "Bürogebäude 1985 (GFK 2020)", "1979–1994"),
    "krankenhaus": ("stundenwerte_haus11_2023.csv", "Krankenhaus/Pflege 1920 (GFK 3051, DRK Westend Haus 11)",
                    "1919–1948"),
}
# which reference series a cooling group uses; groups left out get no indoor estimate (process cooling, halls)
GROUP_REF = {
    "wohnen": "wohnen", "hotel_gastro_heime": "wohnen",
    "krankenhaus_pflege": "krankenhaus",
    "buero_verwaltung": "buero", "bildung_schule": "buero", "hochschule_forschung": "buero", "handel": "buero",
    "kultur_versammlung": "buero", "sport": "buero",
}
# indoor shift per age class against the reference's age class (assumption: lighter construction and more glass
# in newer buildings, heavy masonry before 1949)
AGE_DELTA = {"bis 1918": -0.3, "1919–1948": 0.0, "1949–1957": 0.3, "1958–1968": 0.6, "1969–1978": 0.8,
             "1979–1994": 0.6, "1995–2009": 0.8, "2010–2019": 0.5, "ab 2020": 0.5, "unbekannt": 0.4}
# indoor warming by climate scenario against the reference weather 2023 (assumption), linear in between
CLIMATE_DELTA = [("gering", 2025, 0.0), ("gering", 2045, 0.4), ("gering", 2050, 0.5),
                 ("mittel", 2025, 0.0), ("mittel", 2045, 0.8), ("mittel", 2050, 1.0),
                 ("stark", 2025, 0.0), ("stark", 2045, 1.3), ("stark", 2050, 1.6)]
MEASURES = [
    # measure, label, cool_pct_min, std, max, heat_pct, remark, indoor_delta_k
    ("fenstertausch", "Fenstertausch mit Sonnenschutzverglasung", -20, -10, -3, 0,
     "g-Wert sinkt (etwa 0,75 auf 0,35); Raumwärme läuft über den KWW-Sanierungspfad", -0.8),
    ("sanierung", "energetische Sanierung der Hülle (Dämmung, Fenster)", -15, -5, 10, 0,
     "Wirkung auf Kälte unsicher: Dämmung hält innere und solare Lasten im Gebäude, ohne Nachtlüftung kann der "
     "Bedarf steigen; Raumwärme läuft über den KWW-Sanierungspfad", 0.3),
]
INDOOR_DELTA = {"verschattung_aussen": -1.5, "lueftung_nutzer": -0.5, "nachtlueftung_auto": -2.0}
PET_K = 0.1      # K indoor per K PET deviation from the Berlin mean of built-up blocks (assumption)
PET_MEAN = 36.5  # mean PET 14 h of the built-up blocks (climate model 2022)
PET_CLAMP = 1.0
THRESHOLDS = range(26, 33)
DELTAS = [round(-6 + 0.1 * k, 1) for k in range(141)]


def load_series(path: Path) -> tuple[list[str], list[float]]:
    days, temps = [], []
    with open(path, encoding="utf8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            days.append(r["Beginn_MEZ"][:10])
            temps.append(float(r["T_operativ_C"].replace(",", ".")))
    return days, temps


def lookup_rows(ref: str, days: list[str], temps: list[float]):
    for d in DELTAS:
        day_max: dict[str, float] = defaultdict(lambda: -99.0)
        for day, t in zip(days, temps):
            if t + d > day_max[day]:
                day_max[day] = t + d
        h26 = sum(t + d > 26 for t in temps)
        mx = max(day_max.values())
        for th in THRESHOLDS:
            yield ref, d, th, sum(v > th for v in day_max.values()), h26, round(mx, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn")
    ap.add_argument("--citygml2msl", default=str(Path.home() / ".segberss" / "citygml2msl"))
    a = ap.parse_args()
    base = Path(a.citygml2msl) / RUN
    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute("ALTER TABLE core_bldg.demand_measure ADD COLUMN IF NOT EXISTS indoor_delta_k numeric")
        for m in MEASURES:
            cur.execute("""
                INSERT INTO core_bldg.demand_measure (measure, label, cool_pct_min, cool_pct_std, cool_pct_max,
                    heat_pct, remark, source, indoor_delta_k) VALUES (%s,%s,%s,%s,%s,%s,%s,'Annahme',%s)
                ON CONFLICT (measure) DO UPDATE SET label = EXCLUDED.label, cool_pct_min = EXCLUDED.cool_pct_min,
                    cool_pct_std = EXCLUDED.cool_pct_std, cool_pct_max = EXCLUDED.cool_pct_max,
                    heat_pct = EXCLUDED.heat_pct, remark = EXCLUDED.remark, indoor_delta_k = EXCLUDED.indoor_delta_k""",
                        m)
        for k, v in INDOOR_DELTA.items():
            cur.execute("UPDATE core_bldg.demand_measure SET indoor_delta_k = %s WHERE measure = %s", (v, k))

        cur.execute("""
            DROP TABLE IF EXISTS core_bldg.indoor_lookup, core_bldg.indoor_reference, core_bldg.indoor_group_ref,
                core_bldg.indoor_age_delta, core_bldg.indoor_climate_delta;
            CREATE TABLE core_bldg.indoor_reference (ref text PRIMARY KEY, label text, age_class text, source text);
            CREATE TABLE core_bldg.indoor_group_ref (cooling_group text PRIMARY KEY, ref text);
            CREATE TABLE core_bldg.indoor_age_delta (age_class text PRIMARY KEY, delta_k numeric);
            CREATE TABLE core_bldg.indoor_climate_delta (scenario text, year int, delta_k numeric,
                PRIMARY KEY (scenario, year));
            CREATE TABLE core_bldg.indoor_lookup (ref text, delta_k numeric, threshold_c int, days_over int,
                hours_over_26 int, max_op_c numeric, PRIMARY KEY (ref, delta_k, threshold_c));
            COMMENT ON TABLE core_bldg.indoor_lookup IS
              'Platzhalter: Tage mit maximaler operativer Innentemperatur über threshold_c und Stunden über 26 °C, '
              'wenn die frei schwingende Referenzreihe (citygml2msl grob, DRKK 2023, ohne Kältemaschine) pauschal um '
              'delta_k verschoben wird. Wird durch SimStadt-Simulationen ersetzt.';""")
        for ref, (fn, label, age) in REFS.items():
            cur.execute("INSERT INTO core_bldg.indoor_reference VALUES (%s,%s,%s,%s)",
                        (ref, label, age, f"citygml2msl {RUN}/{fn}"))
            days, temps = load_series(base / fn)
            cur.executemany("INSERT INTO core_bldg.indoor_lookup VALUES (%s,%s,%s,%s,%s,%s)",
                            list(lookup_rows(ref, days, temps)))
        cur.executemany("INSERT INTO core_bldg.indoor_group_ref VALUES (%s,%s)", list(GROUP_REF.items()))
        cur.executemany("INSERT INTO core_bldg.indoor_age_delta VALUES (%s,%s)", list(AGE_DELTA.items()))
        cur.executemany("INSERT INTO core_bldg.indoor_climate_delta VALUES (%s,%s,%s)", CLIMATE_DELTA)

        # building-wise base shift: age (against the reference's age class) + local outdoor heat
        cur.execute(f"""
            DROP TABLE IF EXISTS core_bldg.indoor_base_building;
            CREATE TABLE core_bldg.indoor_base_building AS
            SELECT cb.uuid, g.ref,
                   round(coalesce(ad.delta_k, 0.4) - coalesce(ar.delta_k, 0)
                         + greatest(-{PET_CLAMP}, least({PET_CLAMP}, coalesce(bc.pet14h - {PET_MEAN}, 0) * {PET_K})), 2)
                     AS base_delta_k
            FROM core_bldg.cooling_base_building cb
            JOIN core_bldg.indoor_group_ref g ON g.cooling_group = cb.cooling_group
            JOIN core_bldg.indoor_reference r ON r.ref = g.ref
            LEFT JOIN core_bldg.indoor_age_delta ad ON ad.age_class = cb.age_class
            LEFT JOIN core_bldg.indoor_age_delta ar ON ar.age_class = r.age_class
            LEFT JOIN core_bldg.building_climate bc ON bc.uuid = cb.uuid;
            ALTER TABLE core_bldg.indoor_base_building ADD PRIMARY KEY (uuid);
            COMMENT ON TABLE core_bldg.indoor_base_building IS
              'Platzhalter: Referenzreihe und Grundverschiebung (Baualter + Wärmebelastung am Standort) je Gebäude; '
              'Klima und Maßnahmen kommen zur Abfragezeit dazu.';
            CREATE OR REPLACE FUNCTION core_bldg.f_indoor_climate_delta(p_scenario text, p_year int)
            RETURNS numeric LANGUAGE sql STABLE AS $$
                SELECT CASE WHEN y1.year = y0.year THEN y0.delta_k
                            ELSE y0.delta_k + (y1.delta_k - y0.delta_k) * (p_year - y0.year) / (y1.year - y0.year) END
                FROM (SELECT * FROM core_bldg.indoor_climate_delta WHERE scenario = p_scenario
                        AND year <= greatest(p_year, 2025) ORDER BY year DESC LIMIT 1) y0,
                     (SELECT * FROM core_bldg.indoor_climate_delta WHERE scenario = p_scenario
                        AND year >= least(p_year, 2050) ORDER BY year LIMIT 1) y1 $$;
            CREATE OR REPLACE FUNCTION core_bldg.f_indoor_measure_delta(p_measures text[])
            RETURNS numeric LANGUAGE sql STABLE AS $$
                SELECT coalesce(sum(indoor_delta_k), 0) FROM core_bldg.demand_measure
                WHERE measure = ANY(coalesce(p_measures, '{{}}')) $$;""")
        cur.execute("SELECT ref, count(*), round(avg(base_delta_k), 2) FROM core_bldg.indoor_base_building GROUP BY 1")
        print("Gebäude je Referenz:", cur.fetchall())
        cur.execute("SELECT ref, delta_k, days_over, hours_over_26, max_op_c FROM core_bldg.indoor_lookup "
                    "WHERE threshold_c = 30 AND delta_k IN (0, 1, 2) ORDER BY 1, 2")
        for r in cur.fetchall():
            print("  ", r)


if __name__ == "__main__":
    main()
