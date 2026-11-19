-- Kältebedarf-Mockup, Klimaszenarien, Maßnahmen, Kühl-Sollwerte und Bedarfsfunktion je Gebäude
-- =============================================================================================
-- Platzhalter, bis Simulationsergebnisse der Projektpartner vorliegen. Alle Werte dienen der Darstellung in der
-- Karte; Spalte source = Quelle oder "Annahme". Werte von Hand anpassbar; ein Neulauf dieses Skripts setzt sie zurück.
--
--   core_bldg.cooling_age_class       Baualtersklassen mit Faktor auf den Kältebedarf
--   core_bldg.cooling_spec_demand     kWh/(m²·a) Nutzkälte je GFK und Baualtersklasse (min/std/max),
--                                     witterungsbereinigt, Bezug beheizte Fläche (NGF aus kww_heat_demand_building)
--   core_bldg.climate_scenario        gering / mittel / stark
--   core_bldg.climate_factor          Korrektur Heizen und Kühlen in % je Szenario und Stützjahr (linear interpoliert)
--   core_bldg.demand_measure          Maßnahmen mit Korrektur in % (min/std/max), multiplikativ kombiniert
--   core_bldg.cooling_setpoint        Kühl-Sollwerte 22/24/26 °C, Änderung in % je Kelvin gegenüber dem Bezugs-Sollwert
--   core_bldg.heat_dhw_share          Warmwasseranteil am KWW-Wärmebedarf (Klimafaktor wirkt nur auf die Raumwärme)
--
--   core_bldg.f_building_demand(climate, year, measures[], setpoint, heat_path, uuids[])
--       -> Wärme- und Kältebedarf in kWh/a je beheiztem Gebäude
--
-- Aufruf: psql -v ON_ERROR_STOP=1 --single-transaction -f kaeltebedarf_mockup.sql

-- ---------------------------------------------------------------------------------------------
-- Baualtersklassen
-- ---------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS core_bldg.cooling_age_class CASCADE;
CREATE TABLE core_bldg.cooling_age_class (
    age_class text PRIMARY KEY, year_from int, year_to int, factor numeric NOT NULL, sort int, remark text, source text);
INSERT INTO core_bldg.cooling_age_class VALUES
 ('bis 1918',  NULL, 1918, 0.75, 1, 'Massivbau, hohe Speichermasse, kleiner Fensterflächenanteil', 'Annahme'),
 ('1919–1948', 1919, 1948, 0.85, 2, 'überwiegend Massivbau', 'Annahme'),
 ('1949–1957', 1949, 1957, 0.95, 3, 'Wiederaufbau, leichtere Bauweise', 'Annahme'),
 ('1958–1968', 1958, 1968, 1.15, 4, 'Leichtbau, Vorhangfassaden, große Glasflächen', 'Annahme'),
 ('1969–1978', 1969, 1978, 1.20, 5, 'große Glasflächen, kaum Sonnenschutz, geringe Dämmung', 'Annahme'),
 ('1979–1994', 1979, 1994, 1.05, 6, 'WSchVO 1977/1984', 'Annahme'),
 ('1995–2009', 1995, 2009, 1.00, 7, 'WSchVO 1995/EnEV, Bezugsklasse', 'Annahme'),
 ('2010–2019', 2010, 2019, 1.05, 8, 'dichte, gut gedämmte Hülle, große Glasflächen', 'Annahme'),
 ('ab 2020',   2020, NULL, 1.00, 9, 'GEG mit Nachweis sommerlicher Wärmeschutz (DIN 4108-2)', 'Annahme'),
 ('unbekannt', NULL, NULL, 1.00, 10, 'Baujahr fehlt', 'Annahme');
COMMENT ON TABLE core_bldg.cooling_age_class IS
  'Baualtersklassen für den Kältebedarf-Mockup; factor multipliziert den Grundwert der Nutzungsgruppe. Annahmen.';

CREATE OR REPLACE FUNCTION core_bldg.f_cooling_age_class(p_year int) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT coalesce((SELECT age_class FROM core_bldg.cooling_age_class
                     WHERE p_year IS NOT NULL AND p_year BETWEEN coalesce(year_from, -9999) AND coalesce(year_to, 9999)
                       AND age_class <> 'unbekannt' ORDER BY sort LIMIT 1), 'unbekannt') $$;

-- ---------------------------------------------------------------------------------------------
-- Spezifischer Kältebedarf je GFK und Baualtersklasse
-- ---------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS core_bldg.cooling_spec_demand CASCADE;
CREATE TABLE core_bldg.cooling_spec_demand (
    gfk int, gfk_name text, cooling_group text, age_class text REFERENCES core_bldg.cooling_age_class,
    kwh_m2a_min numeric, kwh_m2a_std numeric, kwh_m2a_max numeric,
    setpoint_base_c numeric, measures_apply boolean, source text, source_setpoint text,
    PRIMARY KEY (gfk, age_class));

WITH grp(cooling_group, mn, st, mx, sp) AS (VALUES
    ('wohnen',                    2,   8,  20, 25),
    ('bildung_schule',            3,  10,  25, 24),
    ('buero_verwaltung',         15,  30,  60, 24),
    ('handel',                   20,  40,  80, 24),
    ('hochschule_forschung',     15,  35,  80, 24),
    ('hotel_gastro_heime',       10,  25,  50, 24),
    ('krankenhaus_pflege',       25,  55, 110, 24),
    ('kultur_versammlung',        8,  20,  45, 24),
    ('lager',                     0,   5,  15, 26),
    ('produktion_gewerbe',        5,  25,  80, 26),
    ('schwimmbad',                3,  10,  25, 24),
    ('sport',                     2,   8,  20, 24),
    ('technik_verkehr_sonstige',  5,  15,  40, 24),
    ('kuehlhaus',                80, 150, 300, NULL)),
ovr(gfk, mn, st, mx) AS (VALUES          -- GFK, für die der Gruppenwert nicht passt
    (2051, 25,  50,  90), (2052, 30,  60, 100),                 -- Kaufhaus, Einkaufszentrum
    (2540, 20,  60, 150),                                       -- Fernmeldewesen (Technikräume)
    (3034, 15,  35,  70), (3037, 10,  25,  50),                 -- Museum, Bibliothek (Bestandsklima)
    (3035, 20,  40,  80),                                       -- Rundfunk, Fernsehen
    (2500, 0, 5, 20), (2510, 0, 5, 20), (2512, 0, 5, 20), (2520, 0, 5, 20), (2570, 0, 5, 20),
    (2600, 0, 5, 20), (2622, 0, 5, 20),                         -- Ver- und Entsorgung
    (2740, 0, 2, 10), (2741, 0, 2, 10), (2742, 0, 2, 10), (3272, 0, 2, 10), (3273, 0, 2, 10))  -- Gewächshäuser
INSERT INTO core_bldg.cooling_spec_demand
SELECT k.gfk, k.gfk_name, g.cooling_group, a.age_class,
       round(coalesce(o.mn, grp.mn) * a.factor, 1), round(coalesce(o.st, grp.st) * a.factor, 1),
       round(coalesce(o.mx, grp.mx) * a.factor, 1),
       grp.sp, g.cooling_group <> 'kuehlhaus', 'Annahme',
       CASE WHEN grp.sp IS NULL THEN NULL
            ELSE 'DIN V 18599-10:2018-09, Nutzungsprofile (Wohnen 25 °C, Lager-/Gewerbehallen 26 °C, sonst 24 °C)' END
FROM core_bldg.kww_gfk_mapping k
JOIN core_bldg.cooling_gfk_group g USING (gfk)
JOIN grp USING (cooling_group)
LEFT JOIN ovr o USING (gfk)
CROSS JOIN core_bldg.cooling_age_class a
WHERE k.heated;

-- Rechenzentren (OSM/PeeringDB, core_bldg.data_center_building) überschreiben die GFK: gfk = 0
INSERT INTO core_bldg.cooling_spec_demand
SELECT 0, 'Rechenzentrum (OSM/PeeringDB, überschreibt GFK)', 'rechenzentrum', a.age_class, 500, 1000, 2500,
       NULL, false, 'Annahme', NULL
FROM core_bldg.cooling_age_class a;

COMMENT ON TABLE core_bldg.cooling_spec_demand IS
  'Mockup Nutzkältebedarf in kWh/(m²·a), witterungsbereinigt, Bezug beheizte Fläche (NGF), je ALKIS-GFK (nur beheizte) und '
  'Baualtersklasse: Grundwert der Nutzungsgruppe (core_bldg.cooling_gfk_group, einzelne GFK abweichend) × Faktor der '
  'Baualtersklasse. gfk = 0: Rechenzentren. setpoint_base_c = Kühl-Sollwert, auf den sich die Werte beziehen. '
  'measures_apply = false: Prozesskälte, Maßnahmen und Sollwert wirken nicht. Platzhalter bis zu Simulationsergebnissen.';

-- ---------------------------------------------------------------------------------------------
-- Klimaszenarien
-- ---------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS core_bldg.climate_factor CASCADE;
DROP TABLE IF EXISTS core_bldg.climate_scenario CASCADE;
CREATE TABLE core_bldg.climate_scenario (scenario text PRIMARY KEY, label text, description text, sort int);
INSERT INTO core_bldg.climate_scenario VALUES
 ('gering', 'geringer Klimawandel', 'Entwicklung etwa wie RCP2.6 / SSP1-2.6', 1),
 ('mittel', 'mittlerer Klimawandel', 'Entwicklung etwa wie RCP4.5 / SSP2-4.5', 2),
 ('stark',  'starker Klimawandel',  'Entwicklung etwa wie RCP8.5 / SSP5-8.5', 3);

CREATE TABLE core_bldg.climate_factor (
    scenario text REFERENCES core_bldg.climate_scenario, year int, heat_pct numeric, cool_pct numeric, source text,
    PRIMARY KEY (scenario, year));
INSERT INTO core_bldg.climate_factor
SELECT s, y, h, c, 'Annahme' FROM (VALUES
 ('gering', 2025, 0, 0), ('gering', 2030, -1,  4), ('gering', 2035, -2,  8), ('gering', 2040, -3, 12),
 ('gering', 2045, -4, 15), ('gering', 2050, -5, 18),
 ('mittel', 2025, 0, 0), ('mittel', 2030, -2,  6), ('mittel', 2035, -4, 13), ('mittel', 2040, -6, 20),
 ('mittel', 2045, -8, 27), ('mittel', 2050, -10, 35),
 ('stark',  2025, 0, 0), ('stark',  2030, -3, 10), ('stark',  2035, -6, 20), ('stark',  2040, -9, 32),
 ('stark',  2045, -12, 45), ('stark',  2050, -15, 60)) v(s, y, h, c);
COMMENT ON TABLE core_bldg.climate_factor IS
  'Klimakorrektur in % gegenüber dem witterungsbereinigten Wert (Bezugsjahr 2025 = 0 %): heat_pct auf die Raumwärme, '
  'cool_pct auf den Kältebedarf. Zwischen den Stützjahren linear interpoliert, außerhalb auf 2025 bzw. 2050 begrenzt. '
  'Platzhalter bis zu Simulationen der Projektpartner.';

CREATE OR REPLACE FUNCTION core_bldg.f_climate_factor(p_scenario text, p_year int, p_kind text) RETURNS numeric
LANGUAGE plpgsql STABLE AS $$
DECLARE y0 int; y1 int; v0 numeric; v1 numeric; y int;
BEGIN
    IF p_kind NOT IN ('heat', 'cool') THEN RAISE EXCEPTION 'p_kind muss heat oder cool sein, nicht %', p_kind; END IF;
    IF NOT EXISTS (SELECT 1 FROM core_bldg.climate_scenario WHERE scenario = p_scenario) THEN
        RAISE EXCEPTION 'Klimaszenario % unbekannt (gering, mittel, stark)', p_scenario; END IF;
    SELECT greatest(min(year), least(max(year), p_year)) INTO y FROM core_bldg.climate_factor WHERE scenario = p_scenario;
    SELECT year, CASE p_kind WHEN 'heat' THEN heat_pct ELSE cool_pct END INTO y0, v0
      FROM core_bldg.climate_factor WHERE scenario = p_scenario AND year <= y ORDER BY year DESC LIMIT 1;
    SELECT year, CASE p_kind WHEN 'heat' THEN heat_pct ELSE cool_pct END INTO y1, v1
      FROM core_bldg.climate_factor WHERE scenario = p_scenario AND year >= y ORDER BY year LIMIT 1;
    RETURN 1 + (CASE WHEN y1 = y0 THEN v0 ELSE v0 + (v1 - v0) * (y - y0) / (y1 - y0) END) / 100;
END $$;

-- ---------------------------------------------------------------------------------------------
-- Maßnahmen
-- ---------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS core_bldg.demand_measure CASCADE;
CREATE TABLE core_bldg.demand_measure (
    measure text PRIMARY KEY, label text,
    cool_pct_min numeric, cool_pct_std numeric, cool_pct_max numeric, heat_pct numeric, remark text, source text);
INSERT INTO core_bldg.demand_measure VALUES
 ('verschattung_aussen', 'außenliegende Fensterverschattung', -40, -25, -10, 0,
  'beweglicher Sonnenschutz, im Winter geöffnet', 'Annahme'),
 ('lueftung_nutzer', 'Lüftung durch Nutzer (Fensterlüftung in kühlen Stunden)', -20, -10, 0, 0,
  'stark abhängig vom Verhalten', 'Annahme'),
 ('nachtlueftung_auto', 'automatische Nachtlüftung', -45, -30, -15, 0,
  'motorische Fenster/Klappen, wirkt mit Speichermasse', 'Annahme');
COMMENT ON TABLE core_bldg.demand_measure IS
  'Maßnahmen mit Korrektur in % auf den Kältebedarf (min = stärkste Wirkung) und die Raumwärme; mehrere Maßnahmen '
  'werden multiplikativ kombiniert. Gilt nicht für Prozesskälte (cooling_spec_demand.measures_apply = false). Annahmen.';

CREATE OR REPLACE FUNCTION core_bldg.f_measure_factor(p_measures text[], p_kind text, p_bound text DEFAULT 'std')
RETURNS numeric LANGUAGE plpgsql STABLE AS $$
DECLARE f numeric := 1; m text; pct numeric;
BEGIN
    FOREACH m IN ARRAY coalesce(p_measures, '{}') LOOP
        SELECT CASE WHEN p_kind = 'heat' THEN heat_pct
                    WHEN p_bound = 'min' THEN cool_pct_min WHEN p_bound = 'max' THEN cool_pct_max ELSE cool_pct_std END
          INTO pct FROM core_bldg.demand_measure WHERE measure = m;
        IF NOT FOUND THEN RAISE EXCEPTION 'Maßnahme % unbekannt (siehe core_bldg.demand_measure)', m; END IF;
        f := f * (1 + pct / 100);
    END LOOP;
    RETURN f;
END $$;

-- ---------------------------------------------------------------------------------------------
-- Kühl-Sollwerte
-- ---------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS core_bldg.cooling_setpoint CASCADE;
CREATE TABLE core_bldg.cooling_setpoint (
    setpoint_c numeric PRIMARY KEY, label text,
    pct_per_k_min numeric, pct_per_k_std numeric, pct_per_k_max numeric, source text);
INSERT INTO core_bldg.cooling_setpoint VALUES
 (22, 'Kühlen auf 22 °C', -15, -12, -8, 'Annahme'),
 (24, 'Kühlen auf 24 °C', -15, -12, -8, 'Annahme'),
 (26, 'Kühlen auf 26 °C', -15, -12, -8, 'Annahme');
COMMENT ON TABLE core_bldg.cooling_setpoint IS
  'Wählbare Kühl-Sollwerte. Änderung des Kältebedarfs je Kelvin über dem Bezugs-Sollwert der Nutzung '
  '(cooling_spec_demand.setpoint_base_c): Faktor = (1 + pct_per_k/100)^(Sollwert − Bezug), z. B. Büro 22 °C = +29 %, '
  '26 °C = −23 %. Annahme.';

CREATE OR REPLACE FUNCTION core_bldg.f_setpoint_factor(p_setpoint numeric, p_base numeric, p_bound text DEFAULT 'std')
RETURNS numeric LANGUAGE plpgsql STABLE AS $$
DECLARE r core_bldg.cooling_setpoint; a numeric; b numeric;
BEGIN
    IF p_setpoint IS NULL OR p_base IS NULL THEN RETURN 1; END IF;
    SELECT * INTO r FROM core_bldg.cooling_setpoint WHERE setpoint_c = p_setpoint;
    IF NOT FOUND THEN RAISE EXCEPTION 'Sollwert % °C nicht vorgesehen (siehe core_bldg.cooling_setpoint)', p_setpoint; END IF;
    IF p_bound = 'std' THEN RETURN power(1 + r.pct_per_k_std / 100, p_setpoint - p_base); END IF;
    a := power(1 + r.pct_per_k_min / 100, p_setpoint - p_base);
    b := power(1 + r.pct_per_k_max / 100, p_setpoint - p_base);
    RETURN CASE WHEN p_bound = 'min' THEN least(a, b) ELSE greatest(a, b) END;
END $$;

-- ---------------------------------------------------------------------------------------------
-- Warmwasseranteil am KWW-Wärmebedarf
-- ---------------------------------------------------------------------------------------------
DROP TABLE IF EXISTS core_bldg.heat_dhw_share CASCADE;
CREATE TABLE core_bldg.heat_dhw_share (kww_type text PRIMARY KEY, dhw_share numeric, source text);
INSERT INTO core_bldg.heat_dhw_share VALUES
 ('ezfh', 0.12, 'Annahme'), ('mfh', 0.18, 'Annahme'), ('buerogebaeude', 0.05, 'Annahme'),
 ('bildungseinrichtungen', 0.05, 'Annahme'), ('handel', 0.05, 'Annahme'),
 ('beherbergung_gaststaetten', 0.25, 'Annahme'), ('krankenhaeuser', 0.20, 'Annahme'),
 ('kultureinrichtungen', 0.05, 'Annahme'), ('sporteinrichtungen', 0.20, 'Annahme'),
 ('schwimmbaeder', 0.30, 'Annahme'), ('industrie', 0.05, 'Annahme'), ('lagergebaeude', 0.02, 'Annahme');
COMMENT ON TABLE core_bldg.heat_dhw_share IS
  'Anteil Warmwasser am KWW-Wärmebedarf (Heizung + WW) je KWW-Typ; die Klimakorrektur wirkt nur auf den Rest. Annahmen.';

-- ---------------------------------------------------------------------------------------------
-- Wärme- und Kältebedarf je Gebäude
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core_bldg.f_building_demand(
    p_climate   text    DEFAULT 'mittel',      -- gering | mittel | stark
    p_year      int     DEFAULT 2025,          -- Bilanzjahr
    p_measures  text[]  DEFAULT '{}',          -- z. B. '{verschattung_aussen,nachtlueftung_auto}'
    p_setpoint  numeric DEFAULT NULL,          -- 22 | 24 | 26 °C, NULL = Bezugs-Sollwert der Nutzung
    p_heat_path text    DEFAULT '2045_o45',    -- KWW-Pfad für die Sanierung bis 2045: 2045_o45 | 2045_uba | 2045_fortschreibung
    p_uuids     text[]  DEFAULT NULL)          -- NULL = alle beheizten Gebäude
RETURNS TABLE (
    uuid varchar, gfk int, cooling_group text, age_class text, ngf_m2 numeric,
    cooling_kwh_m2a_std numeric, cooling_kwh_min numeric, cooling_kwh_std numeric, cooling_kwh_max numeric,
    heat_kwh_base numeric, heat_kwh numeric)
LANGUAGE plpgsql STABLE AS $$
#variable_conflict use_column
DECLARE
    c_cool numeric := core_bldg.f_climate_factor(p_climate, p_year, 'cool');
    c_heat numeric := core_bldg.f_climate_factor(p_climate, p_year, 'heat');
    m_min numeric := core_bldg.f_measure_factor(p_measures, 'cool', 'min');
    m_std numeric := core_bldg.f_measure_factor(p_measures, 'cool', 'std');
    m_max numeric := core_bldg.f_measure_factor(p_measures, 'cool', 'max');
    m_heat numeric := core_bldg.f_measure_factor(p_measures, 'heat');
    t numeric := least(greatest((p_year - 2025) / 20.0, 0), 1);   -- Anteil des Sanierungspfads 2025 -> 2045
BEGIN
    IF p_heat_path NOT IN (SELECT scenario FROM core_bldg.kww_scenario WHERE scenario <> '2025') THEN
        RAISE EXCEPTION 'Wärmepfad % unbekannt (siehe core_bldg.kww_scenario)', p_heat_path; END IF;
    PERFORM core_bldg.f_setpoint_factor(p_setpoint, 24);           -- prüft den Sollwert
    RETURN QUERY
    SELECT h.uuid, h.gfk, s.cooling_group, s.age_class, h.ngf_m2, s.kwh_m2a_std,
           round(h.ngf_m2 * s.kwh_m2a_min * c_cool
                 * CASE WHEN s.measures_apply THEN m_min * core_bldg.f_setpoint_factor(p_setpoint, s.setpoint_base_c, 'min') ELSE 1 END),
           round(h.ngf_m2 * s.kwh_m2a_std * c_cool
                 * CASE WHEN s.measures_apply THEN m_std * core_bldg.f_setpoint_factor(p_setpoint, s.setpoint_base_c, 'std') ELSE 1 END),
           round(h.ngf_m2 * s.kwh_m2a_max * c_cool
                 * CASE WHEN s.measures_apply THEN m_max * core_bldg.f_setpoint_factor(p_setpoint, s.setpoint_base_c, 'max') ELSE 1 END),
           round(h.heat_dhw_kwh_a),
           round((h.heat_dhw_kwh_a + t * (coalesce(h2.heat_dhw_kwh_a, h.heat_dhw_kwh_a) - h.heat_dhw_kwh_a))
                 * (coalesce(d.dhw_share, 0.1) + (1 - coalesce(d.dhw_share, 0.1)) * c_heat) * m_heat)
    FROM core_bldg.kww_heat_demand_building h
    JOIN core_bldg.kww_gfk_mapping k ON k.gfk = h.gfk AND k.heated
    LEFT JOIN core_bldg.kww_heat_demand_building h2 ON h2.uuid = h.uuid AND h2.scenario = p_heat_path
    LEFT JOIN core_bldg.heat_dhw_share d ON d.kww_type = h.kww_type
    JOIN core_bldg.cooling_spec_demand s
      ON s.gfk = CASE WHEN EXISTS (SELECT 1 FROM core_bldg.data_center_building dc WHERE dc.uuid = h.uuid) THEN 0 ELSE h.gfk END
     AND s.age_class = core_bldg.f_cooling_age_class(h.baujahr)
    WHERE h.scenario = '2025' AND h.ngf_m2 > 0
      AND (p_uuids IS NULL OR h.uuid = ANY (p_uuids));
END $$;
COMMENT ON FUNCTION core_bldg.f_building_demand(text, int, text[], numeric, text, text[]) IS
  'Wärme- und Kältebedarf je beheiztem Gebäude in kWh/a für Klimaszenario, Jahr, Maßnahmen und Kühl-Sollwert. '
  'Kälte = NGF × cooling_spec_demand × Klimafaktor × Maßnahmen × Sollwertfaktor (min/std/max). '
  'Wärme = KWW-Wärmebedarf (Endenergie Heizung + WW), zwischen 2025 und dem Pfad 2045 linear, Klimafaktor nur auf den '
  'Raumwärmeanteil, × Maßnahmen. Alles Platzhalter bis zu den Simulationen der Projektpartner.';
