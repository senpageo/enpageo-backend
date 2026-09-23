-- Generic per-usage-type cooling estimate, replacing the per-building calib_monthly table
-- (deleted: 36 GB for 221/222 unused climate/refurbishment combinations). Heating stays
-- building-specific via emp.emblive; cooling has no remaining real per-building source, so
-- this is a deliberately simple placeholder: annual specific demand + capacity per usage
-- type, spread across the year with one shared seasonal shape (Berlin cooling season,
-- May-Sep). Not a simulation result -- swap in a real one later if it appears.

CREATE TABLE IF NOT EXISTS core_bldg.usage_cooling_profile (
    usage_zone_type TEXT PRIMARY KEY,
    specific_annual_cooling_kwh_m2 NUMERIC NOT NULL,
    specific_cooling_capacity_w_m2 NUMERIC NOT NULL,
    pct_01 NUMERIC NOT NULL DEFAULT 0,
    pct_02 NUMERIC NOT NULL DEFAULT 0,
    pct_03 NUMERIC NOT NULL DEFAULT 0,
    pct_04 NUMERIC NOT NULL DEFAULT 0,
    pct_05 NUMERIC NOT NULL DEFAULT 10,
    pct_06 NUMERIC NOT NULL DEFAULT 20,
    pct_07 NUMERIC NOT NULL DEFAULT 30,
    pct_08 NUMERIC NOT NULL DEFAULT 30,
    pct_09 NUMERIC NOT NULL DEFAULT 10,
    pct_10 NUMERIC NOT NULL DEFAULT 0,
    pct_11 NUMERIC NOT NULL DEFAULT 0,
    pct_12 NUMERIC NOT NULL DEFAULT 0,
    CONSTRAINT pct_sums_to_100 CHECK (
        abs((pct_01+pct_02+pct_03+pct_04+pct_05+pct_06+pct_07+pct_08+pct_09+pct_10+pct_11+pct_12) - 100) < 0.01
    )
);

-- annual kWh/m2, capacity W/m2 -- both "plausible", not measured. Monthly shape (May-Sep,
-- 10/20/30/30/10) uses the table defaults above for every row.
INSERT INTO core_bldg.usage_cooling_profile (usage_zone_type, specific_annual_cooling_kwh_m2, specific_cooling_capacity_w_m2) VALUES
    ('Einfamilien-/ Reihenhaus', 8, 25),
    ('Unbeheizt', 0, 0),
    ('(Grosses) Mehrfamilienhaus', 10, 25),
    ('Produktions-, Werkstatt-, Lager- oder Betriebsgebaeude', 20, 50),
    ('Handelsgebaeude', 45, 85),
    ('Freizeit mit Tagesnutzung', 25, 50),
    ('Buero-, Verwaltungs- oder Amtsgebaeude', 40, 70),
    ('Schule, Kindertagesstaette und sonstige Betreuungsgebaeude', 15, 40),
    ('Beherberungs- oder Unterbringungsgebaeude', 30, 55),
    ('Sporthalle', 12, 35),
    ('Technikgebaeude (Ver- und Entsorgung)', 15, 40),
    ('Gebaeude fuer Forschung und Hochschullehre', 45, 75),
    ('Produktions- und Technikgebaeude', 20, 50),
    ('Gastronomie- oder Verpflegungsgebaeude', 50, 95),
    ('Pflegeheim', 35, 55),
    ('Verkehrsgebaeude', 15, 35),
    ('Krankenhaus', 55, 100),
    ('Polizei', 35, 60),
    ('Arztpraxen', 35, 60),
    ('Feuerwehr', 20, 40),
    ('Freizeit mit Abendnutzung', 25, 50),
    ('Schwimmbad', 40, 70)
ON CONFLICT (usage_zone_type) DO NOTHING;

-- Fallback row for buildings whose usage_zone_type is blank/unrecognised (LEFT JOIN misses):
-- handled in the query with COALESCE(...) against these generic values, not a table row,
-- since '' can't be a meaningful key alongside the real category strings.
