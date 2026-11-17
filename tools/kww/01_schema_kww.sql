-- KWW-Technikkatalog Wärmeplanung (08-2026, Version 1.1), Tab 53–56
-- Gebäudedaten (Endenergie Heizung + Warmwasser je BGF) und Prozesswärme-Relevanz
-- Ziel: core_bldg. Wiederholbar: Tabellen werden neu angelegt, Inhalte kommen aus
-- 02_import_kww.py (Excel) und 03_gfk_kww_mapping.sql (Zuordnung ALKIS-GFK).
--
-- Rechenweg (siehe kww_waermebedarf.py):
--   BGF            = Grundfläche (ALKIS-Umring) * Anzahl oberirdischer Geschosse (aog)
--   Bedarf [kWh/a] = BGF * spezifischer Wert [kWh/(m² BGF*a)] (Gebäudetyp, Baualtersklasse, Szenario)
--   NGF (nur Info) = BGF / Umrechnungsfaktor BGF/NGF des Gebäudetyps
-- Die KWW-Werte sind schon auf BGF bezogen, der Faktor darf also nicht noch einmal
-- angewendet werden.

BEGIN;

DROP TABLE IF EXISTS core_bldg.kww_gfk_mapping CASCADE;
DROP TABLE IF EXISTS core_bldg.kww_spec_demand CASCADE;
DROP TABLE IF EXISTS core_bldg.kww_reduction CASCADE;
DROP TABLE IF EXISTS core_bldg.kww_process_heat CASCADE;
DROP TABLE IF EXISTS core_bldg.kww_bldg_type CASCADE;
DROP TABLE IF EXISTS core_bldg.kww_scenario CASCADE;
DROP TABLE IF EXISTS core_bldg.kww_note CASCADE;

CREATE TABLE core_bldg.kww_note (
    code        text PRIMARY KEY,            -- z. B. 'HA', '[63]'
    kind        text NOT NULL CHECK (kind IN ('anmerkung', 'quelle')),
    text        text NOT NULL
);
COMMENT ON TABLE core_bldg.kww_note IS
  'Anmerkungen (HA…HS) und Quellen ([14], [63]…[67]) aus KWW-Technikkatalog Tab 53–56';

CREATE TABLE core_bldg.kww_scenario (
    scenario    text PRIMARY KEY,            -- '2025', '2045_o45', '2045_uba', '2045_fortschreibung'
    label       text NOT NULL,               -- Spaltenkopf im Katalog
    target_year smallint NOT NULL,
    sort        smallint NOT NULL
);
COMMENT ON TABLE core_bldg.kww_scenario IS
  'Szenarien der KWW-Gebäudedaten: Ausgangswert 2025 und drei Pfade bis 2045';

CREATE TABLE core_bldg.kww_bldg_type (
    kww_type        text PRIMARY KEY,        -- Schlüssel wie in flatdata_all, z. B. 'schwimmbaeder', 'mfh'
    label           text NOT NULL,           -- Gebäudetyp laut Katalog
    sector          text NOT NULL CHECK (sector IN ('NWG', 'WG')),
    kww_tab         text NOT NULL,           -- 'Tab 53' | 'Tab 54' | 'Tab 55'
    bgf_ngf_factor  numeric,                 -- Bruttogeschossfläche / Nutzfläche
    note_codes      text,                    -- Anmerkungen zur Zeile 'Alle Klassen'
    sort            smallint NOT NULL
);
COMMENT ON TABLE core_bldg.kww_bldg_type IS
  'Gebäudetypen der KWW-Gebäudedaten (Tab 53 NWG, Tab 54 EZFH, Tab 55 MFH) mit Umrechnungsfaktor BGF/NGF';
COMMENT ON COLUMN core_bldg.kww_bldg_type.bgf_ngf_factor IS
  'Umrechnungsfaktor BGF/Nutzfläche nach Mannek [64]; die spezifischen Werte sind bereits auf BGF bezogen';

CREATE TABLE core_bldg.kww_spec_demand (
    kww_type         text NOT NULL REFERENCES core_bldg.kww_bldg_type,
    age_class        text NOT NULL,          -- wie im Katalog, z. B. 'bis 1978', '1979–2009', 'Alle Klassen'
    year_from        smallint,               -- NULL = offen nach unten bzw. 'Alle Klassen'
    year_to          smallint,               -- NULL = offen nach oben bzw. 'Alle Klassen'
    scenario         text NOT NULL REFERENCES core_bldg.kww_scenario,
    kwh_per_m2_bgf_a numeric,                -- NULL = 'k. A.' im Katalog
    note_codes       text,
    source_codes     text,
    PRIMARY KEY (kww_type, age_class, scenario)
);
COMMENT ON TABLE core_bldg.kww_spec_demand IS
  'Endenergieverbrauch je Bruttogeschossfläche für Heizung und Warmwasser [kWh/(m²*a)], je Gebäudetyp, Baualtersklasse und Szenario (KWW Tab 53–55). Ohne Prozesswärme.';

CREATE TABLE core_bldg.kww_reduction (
    kww_type              text NOT NULL REFERENCES core_bldg.kww_bldg_type,
    scenario              text NOT NULL REFERENCES core_bldg.kww_scenario,
    annual_reduction_pct  numeric,           -- 'Mittlere jährliche Reduktion' [%/a]
    level_2045_pct        numeric,           -- 'Reduktion bis 2045 auf (2025 = 100 %)'
    note_codes            text,
    PRIMARY KEY (kww_type, scenario)
);
COMMENT ON TABLE core_bldg.kww_reduction IS
  'Reduktionspfade je Gebäudetyp und Szenario aus den Tabellenblättern (nicht aus flatdata_all, dort weichen einzelne NWG-Werte ab)';

CREATE TABLE core_bldg.kww_process_heat (
    function_label        text PRIMARY KEY,  -- 'Gebäudefunktion' laut Tab 56
    ghd_type_schlomann    text,
    ghd_type_iwu          text,
    process_heat          text,              -- 'Ja' | 'Nein' | 'Ja/Nein' (je nach Betriebsart)
    data_collection       text,              -- 'Ja' | 'Nein' | 'Ja/Nein'
    precondition          text,              -- Spalte 'Voraussetzung'
    sort                  smallint NOT NULL
);
COMMENT ON TABLE core_bldg.kww_process_heat IS
  'KWW Tab 56: Relevanz der Prozesswärme und Empfehlung zur Datenerfassung je Gebäudefunktion. Enthält KEINE spezifischen Bedarfswerte.';

CREATE TABLE core_bldg.kww_gfk_mapping (
    gfk                    integer PRIMARY KEY,   -- ALKIS Gebäudefunktion (AX_Gebaeudefunktion)
    gfk_name               text NOT NULL,
    heated                 boolean NOT NULL,      -- false = kein Heizwärmebedarf (übernommen aus emc.def_bldg_function_gfk)
    kww_type               text REFERENCES core_bldg.kww_bldg_type,  -- NULL = kein passender KWW-Typ
    residential_split      boolean NOT NULL DEFAULT false,  -- true = EZFH/MFH aus EnergyMap-Typologie bzw. Geschosszahl
    mapping_quality        text NOT NULL CHECK (mapping_quality IN ('direkt', 'naeherung', 'kein_typ', 'unbeheizt')),
    process_heat_function  text REFERENCES core_bldg.kww_process_heat,  -- Zeile aus Tab 56, NULL = nicht aufgeführt
    remark                 text
);
COMMENT ON TABLE core_bldg.kww_gfk_mapping IS
  'Zuordnung ALKIS-Gebäudefunktion (GFK, vierstellig) zu KWW-Gebäudetyp (Tab 53–55) und Prozesswärme-Funktion (Tab 56). Handgepflegt, Stand 2026-11-16.';

COMMIT;
