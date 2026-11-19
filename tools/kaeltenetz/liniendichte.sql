-- Wärme- und Kälteliniendichten auf der potenziellen Netzstruktur (core_base.v_potential_network)
--
-- Liniendichte = Jahresbedarf der an einen Hauptnetz-Abschnitt angeschlossenen Gebäude je Meter Leitung, in kWh/(m·a)
-- (Einheit wie die Berliner Wärmeliniendichten der Bestandsanalyse zum Wärmeplan 2026).
--   *_kwh_m_a          bezogen auf die Länge des Straßenabschnitts (Hauptleitung) – vergleichbar mit Berlin
--   *_kwh_m_a_trasse   bezogen auf Hauptleitung + alle Anschlussleitungen des Abschnitts (gesamte Trassenlänge)
-- Bedarfe aus core_bldg.f_building_demand (Wärme: KWW Heizung + TWW, Endenergie; Kälte: Mockup mit Spanne).
-- Kälte ist Platzhalter (Annahme), bis Simulationsergebnisse der Projektpartner vorliegen.
--
-- Aufruf beliebiges Szenario:  SELECT * FROM core_bldg.f_line_density('stark', 2045, '{nachtlueftung_auto}', 26);
-- Voreingestellte Szenarien:   core_bldg.line_density (CALL core_bldg.p_refresh_line_density(); ~1 min)
-- Karte:                       core_base.v_line_density (mit Geometrie)

DROP VIEW IF EXISTS core_base.v_line_density;
DROP PROCEDURE IF EXISTS core_bldg.p_refresh_line_density();
DROP TABLE IF EXISTS core_bldg.line_density;
DROP TABLE IF EXISTS core_bldg.line_density_scenario;
DROP FUNCTION IF EXISTS core_bldg.f_line_density(text, int, text[], numeric, text);

CREATE FUNCTION core_bldg.f_line_density(
    p_climate text DEFAULT 'mittel', p_year int DEFAULT 2025, p_measures text[] DEFAULT '{}',
    p_setpoint numeric DEFAULT NULL, p_heat_path text DEFAULT '2045_o45')
RETURNS TABLE (segment_id int, n_buildings int, n_without_demand int, street_m numeric, connection_m numeric,
               heat_mwh_a numeric, cooling_mwh_a numeric, cooling_min_mwh_a numeric, cooling_max_mwh_a numeric,
               heat_kwh_m_a numeric, cooling_kwh_m_a numeric, cooling_min_kwh_m_a numeric, cooling_max_kwh_m_a numeric,
               heat_kwh_m_a_trasse numeric, cooling_kwh_m_a_trasse numeric)
LANGUAGE sql STABLE AS $$
    WITH d AS (SELECT * FROM core_bldg.f_building_demand(p_climate, p_year, p_measures, p_setpoint, p_heat_path)),
    agg AS (
        SELECT c.segment_id, count(*)::int AS n, count(*) FILTER (WHERE d.uuid IS NULL)::int AS n0,
               sum(c.length_m) AS conn_m,
               coalesce(sum(d.heat_kwh), 0) AS heat, coalesce(sum(d.cooling_kwh_std), 0) AS cool,
               coalesce(sum(d.cooling_kwh_min), 0) AS cool_min, coalesce(sum(d.cooling_kwh_max), 0) AS cool_max
        FROM core_bldg.network_connection c
        LEFT JOIN d ON d.uuid = c.uuid
        GROUP BY c.segment_id)
    SELECT a.segment_id, a.n, a.n0, s.length_m, round(a.conn_m, 1),
           round(a.heat / 1000, 1), round(a.cool / 1000, 1), round(a.cool_min / 1000, 1), round(a.cool_max / 1000, 1),
           round(a.heat / nullif(s.length_m, 0)), round(a.cool / nullif(s.length_m, 0)),
           round(a.cool_min / nullif(s.length_m, 0)), round(a.cool_max / nullif(s.length_m, 0)),
           round(a.heat / nullif(s.length_m + a.conn_m, 0)), round(a.cool / nullif(s.length_m + a.conn_m, 0))
    FROM agg a JOIN core_base.street_segment s ON s.id = a.segment_id
$$;

COMMENT ON FUNCTION core_bldg.f_line_density(text, int, text[], numeric, text) IS
  'Wärme- und Kälteliniendichte je Hauptnetz-Abschnitt der potenziellen Netzstruktur in kWh/(m·a), für ein beliebiges '
  'Szenario (Parameter wie core_bldg.f_building_demand). *_kwh_m_a: je Meter Straßenabschnitt (wie Berliner '
  'Wärmeliniendichte); *_trasse: je Meter Hauptleitung + Anschlussleitungen. n_without_demand: angeschlossene Gebäude '
  'ohne Bedarfswert (keine KWW-Fläche). Kälte = Mockup (Annahme).';

CREATE TABLE core_bldg.line_density_scenario (
    scenario text PRIMARY KEY, label text, climate text, year int, measures text[], setpoint numeric, heat_path text,
    sort int);
INSERT INTO core_bldg.line_density_scenario VALUES
  ('standard',                'Standardszenario 2030, mittlerer Klimawandel, außenliegende Fensterverschattung',
                              'mittel', 2030, '{verschattung_aussen}', NULL, '2045_o45', 0),
  ('2025',                    'Heute (2025)',                                        'mittel', 2025, '{}', NULL, '2045_o45', 1),
  ('2045_gering',             '2045, geringer Klimawandel',                          'gering', 2045, '{}', NULL, '2045_o45', 2),
  ('2045_mittel',             '2045, mittlerer Klimawandel',                         'mittel', 2045, '{}', NULL, '2045_o45', 3),
  ('2045_stark',              '2045, starker Klimawandel',                           'stark',  2045, '{}', NULL, '2045_o45', 4),
  ('2045_stark_massnahmen',   '2045, starker Klimawandel, alle Maßnahmen, Sollwert 26 °C', 'stark', 2045,
                              '{verschattung_aussen,lueftung_nutzer,nachtlueftung_auto}', 26, '2045_o45', 5);
COMMENT ON TABLE core_bldg.line_density_scenario IS
  'Voreingestellte Szenarien für core_bldg.line_density (Parameter von core_bldg.f_building_demand; setpoint NULL = '
  'Normsollwert der Nutzung, heat_path = KWW-Pfad für die Wärme).';

CREATE TABLE core_bldg.line_density (
    scenario text REFERENCES core_bldg.line_density_scenario ON DELETE CASCADE,
    segment_id int, n_buildings int, n_without_demand int, street_m numeric, connection_m numeric,
    heat_mwh_a numeric, cooling_mwh_a numeric, cooling_min_mwh_a numeric, cooling_max_mwh_a numeric,
    heat_kwh_m_a numeric, cooling_kwh_m_a numeric, cooling_min_kwh_m_a numeric, cooling_max_kwh_m_a numeric,
    heat_kwh_m_a_trasse numeric, cooling_kwh_m_a_trasse numeric,
    PRIMARY KEY (scenario, segment_id));
COMMENT ON TABLE core_bldg.line_density IS
  'Wärme- und Kälteliniendichten je Hauptnetz-Abschnitt für die Szenarien in core_bldg.line_density_scenario '
  '(berechnet mit core_bldg.f_line_density, neu mit CALL core_bldg.p_refresh_line_density()). Kälte = Mockup (Annahme).';

CREATE PROCEDURE core_bldg.p_refresh_line_density() LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
    DELETE FROM core_bldg.line_density;
    FOR r IN SELECT * FROM core_bldg.line_density_scenario ORDER BY sort LOOP
        INSERT INTO core_bldg.line_density
        SELECT r.scenario, f.* FROM core_bldg.f_line_density(r.climate, r.year, r.measures, r.setpoint, r.heat_path) f;
    END LOOP;
END $$;

CALL core_bldg.p_refresh_line_density();
ANALYZE core_bldg.line_density;

CREATE VIEW core_base.v_line_density AS
SELECT l.*, sc.label, s.street_name, s.district, s.geom
FROM core_bldg.line_density l
JOIN core_bldg.line_density_scenario sc USING (scenario)
JOIN core_base.street_segment s ON s.id = l.segment_id;
COMMENT ON VIEW core_base.v_line_density IS
  'Wärme- und Kälteliniendichten mit Geometrie der Hauptnetz-Abschnitte; für die Karte nach scenario filtern.';
