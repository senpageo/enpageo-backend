-- Basiswerte des Kältebedarf-Mockups je Gebäude, damit die Chat-Werkzeuge jedes Szenario schnell rechnen können
--
-- core_bldg.cooling_base_building: Kältebedarf 2025 ohne Klimawandel, ohne Maßnahmen, beim Normsollwert der Nutzung
-- (= core_bldg.f_building_demand('mittel', 2025)), dazu was die Szenariofaktoren brauchen (measures_apply,
-- setpoint_base_c) und die Volllaststunden für die benötigte Kälteleistung.
-- Ein Szenario ergibt sich je Gebäude als
--   kwh_<bound> × Klimafaktor(Jahr, Szenario)
--               × [wenn measures_apply] Maßnahmenfaktor(<bound>) × (1 + pct_per_k/100)^(Sollwert − setpoint_base_c)
-- also dieselbe Rechnung wie f_building_demand, nur ohne die Funktion je Gebäude aufzurufen (ganz Berlin < 1 s).
-- Benötigte Kälteleistung (kW) = Kältebedarf / vlh_mean (core_bldg.cooling_usage_group), ohne Überdimensionierung.
--
-- Kältestrom, wenn der Bedarf mit Kompressionskälte gedeckt wird: Kältebedarf / Jahres-Leistungszahl (EER) der
-- Nutzungsgruppe (core_bldg.cooling_eer_group); elektrische Leistung = benötigte Kälteleistung / EER.
--
-- Neu berechnen nach Änderungen am Mockup oder am KWW-Bestand: dieses Skript erneut ausführen.

DROP TABLE IF EXISTS core_bldg.cooling_base_building;
DROP TABLE IF EXISTS core_bldg.cooling_eer_group;
CREATE TABLE core_bldg.cooling_eer_group (
    cooling_group text PRIMARY KEY, system text, eer_min numeric, eer_mean numeric, eer_max numeric, source text);
-- große Anlagen meist wassergekühlt (Kühlturm), sonst luftgekühlt; Werte wie core_bldg.chiller_type_param (Barth)
INSERT INTO core_bldg.cooling_eer_group
SELECT g.cooling_group,
       CASE WHEN g.cooling_group IN ('rechenzentrum', 'kuehlhaus', 'produktion_gewerbe', 'krankenhaus_pflege')
            THEN 'wassergekühlt' ELSE 'luftgekühlt' END,
       CASE WHEN g.cooling_group IN ('rechenzentrum', 'kuehlhaus', 'produktion_gewerbe', 'krankenhaus_pflege') THEN 3.3 ELSE 2.1 END,
       CASE WHEN g.cooling_group IN ('rechenzentrum', 'kuehlhaus', 'produktion_gewerbe', 'krankenhaus_pflege') THEN 3.9 ELSE 2.7 END,
       CASE WHEN g.cooling_group IN ('rechenzentrum', 'kuehlhaus', 'produktion_gewerbe', 'krankenhaus_pflege') THEN 4.5 ELSE 3.3 END,
       'Annahme: Anlagentyp je Nutzung; EER Barth Diss. (wassergekühlt 3,9 ± 0,6, luftgekühlt 2,7 ± 0,6)'
FROM core_bldg.cooling_usage_group g;
COMMENT ON TABLE core_bldg.cooling_eer_group IS
  'Angenommene Jahres-Leistungszahl der Kälteerzeugung je Nutzungsgruppe für den Kältestrom aus dem Kältebedarf.';
CREATE TABLE core_bldg.cooling_base_building AS
SELECT h.uuid::text AS uuid, h.gfk, s.gfk AS spec_gfk, s.cooling_group, s.age_class, h.ngf_m2,
       s.kwh_m2a_min, s.kwh_m2a_std, s.kwh_m2a_max,
       round(h.ngf_m2 * s.kwh_m2a_min) AS kwh_min,
       round(h.ngf_m2 * s.kwh_m2a_std) AS kwh_std,
       round(h.ngf_m2 * s.kwh_m2a_max) AS kwh_max,
       s.measures_apply, s.setpoint_base_c, g.vlh_mean, e.eer_min, e.eer_mean, e.eer_max
FROM core_bldg.kww_heat_demand_building h
JOIN core_bldg.kww_gfk_mapping k ON k.gfk = h.gfk AND k.heated
JOIN core_bldg.cooling_spec_demand s
  ON s.gfk = CASE WHEN EXISTS (SELECT 1 FROM core_bldg.data_center_building dc WHERE dc.uuid = h.uuid) THEN 0 ELSE h.gfk END
 AND s.age_class = core_bldg.f_cooling_age_class(h.baujahr)
LEFT JOIN core_bldg.cooling_usage_group g ON g.cooling_group = s.cooling_group
LEFT JOIN core_bldg.cooling_eer_group e ON e.cooling_group = s.cooling_group
WHERE h.scenario = '2025' AND h.ngf_m2 > 0;

ALTER TABLE core_bldg.cooling_base_building ADD PRIMARY KEY (uuid);
CREATE INDEX ON core_bldg.cooling_base_building (cooling_group);
ANALYZE core_bldg.cooling_base_building;

COMMENT ON TABLE core_bldg.cooling_base_building IS
  'Kältebedarf-Mockup je beheiztem Gebäude als Basis (2025, ohne Klimawandel, ohne Maßnahmen, Normsollwert): '
  'kwh_min/std/max = NGF (KWW) × kWh/(m²·a) aus core_bldg.cooling_spec_demand (GFK × Baualtersklasse, Rechenzentren '
  'über core_bldg.data_center_building als GFK 0). Szenarien = Basis × Klima-, Maßnahmen- und Sollwertfaktor wie '
  'core_bldg.f_building_demand. vlh_mean: Volllaststunden je Nutzungsgruppe für die benötigte Kälteleistung; eer_*: Leistungszahl für den Kältestrom. Annahme.';
