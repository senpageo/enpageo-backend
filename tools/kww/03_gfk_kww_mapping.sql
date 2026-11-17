-- Zuordnung ALKIS-Gebäudefunktion (GFK) -> KWW-Gebäudetyp (Tab 53–55) und Prozesswärme-Funktion (Tab 56)
-- Alle 188 GFK-Codes, die in emc.building (Berlin) vorkommen. Stand 2026-11-16, handgepflegt.
--
-- kww_type NULL     = kein sinnvoller KWW-Typ (z. B. Gewächshaus, Technikgebäude): Bedarf bleibt leer statt geraten
-- residential_split = Wohngebäude: EZFH (EnergyMap-Typ EFH/RH) bzw. MFH (MFH/GMH/HH); ohne EnergyMap-Typ: aog <= 2 -> EZFH
-- heated            = aus emc.def_bldg_function_gfk (heated = false -> unbeheizt), sonst beheizt
-- ph                = Anfang des Funktionsnamens in Tab 56 (wird unten eindeutig aufgelöst)

BEGIN;
DELETE FROM core_bldg.kww_gfk_mapping;

WITH m(gfk, kww_type, split, quality, ph, remark) AS (VALUES
  -- Wohnen
  (1000, NULL::text, true,  'direkt',    NULL::text, NULL::text),
  (1002, NULL,       true,  'naeherung', NULL,       'GFK nicht im amtlichen Katalog, wie Wohngebäude behandelt'),
  (1010, NULL,       true,  'direkt',    NULL,       NULL),
  (1020, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (1021, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (1022, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (1023, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (1024, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (1025, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (1100, 'mfh', false, 'naeherung', NULL, 'gemischt genutzt, Wohnanteil als MFH'),
  (1110, 'mfh', false, 'naeherung', NULL, 'gemischt genutzt, Wohnanteil als MFH'),
  (1120, 'mfh', false, 'naeherung', NULL, 'Wohnen mit Handel/DL (meist EG-Laden), als MFH'),
  (1123, 'mfh', false, 'naeherung', NULL, 'Wohn- und Geschäftsgebäude, als MFH'),
  (1130, 'mfh', false, 'naeherung', NULL, 'Wohnen mit Gewerbe, als MFH'),
  (1210, 'mfh', false, 'naeherung', NULL, NULL),
  (1220, 'mfh', false, 'naeherung', 'Land- und forstwirtschaftliches Betriebsgebäude', NULL),
  (1223, 'ezfh', false, 'direkt',   NULL, NULL),
  (1310, 'ezfh', false, 'naeherung', NULL, 'Freizeit-Wohngebäude (überwiegend Kleingarten/Wochenende)'),
  (1311, 'ezfh', false, 'naeherung', NULL, NULL),
  (1312, 'ezfh', false, 'naeherung', NULL, 'Wochenendhaus: KWW-Wert gilt für ganzjährig bewohnte EZFH, Bedarf vermutlich überschätzt'),
  (1313, NULL,  false, 'unbeheizt', NULL, NULL),
  -- Handel, Büro, Beherbergung, Bewirtung, Freizeit
  (2000, 'industrie', false, 'naeherung', 'Gebäude für Wirtschaft oder Gewerbe', 'Sammelklasse, in Berlin oft kleine Gewerbebauten'),
  (2010, 'handel', false, 'direkt', 'Gebäude für Handel und Dienstleistungen', NULL),
  (2020, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (2030, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (2040, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (2050, 'handel', false, 'direkt', 'Gebäude für Handel und Dienstleistungen', NULL),
  (2051, 'handel', false, 'direkt', 'Gebäude für Handel und Dienstleistungen', NULL),
  (2052, 'handel', false, 'direkt', 'Gebäude für Handel und Dienstleistungen', NULL),
  (2053, 'handel', false, 'unbeheizt', 'Hallen', 'Markthalle in EnergyMap unbeheizt'),
  (2054, 'handel', false, 'direkt', 'Gebäude für Handel und Dienstleistungen', NULL),
  (2055, 'handel', false, 'direkt', 'Gebäude für Handel und Dienstleistungen', NULL),
  (2060, 'kultureinrichtungen', false, 'naeherung', 'Hallen', 'Messehalle'),
  (2070, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (2071, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (2072, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (2074, NULL, false, 'unbeheizt', NULL, NULL),
  (2080, 'beherbergung_gaststaetten', false, 'direkt', 'Gebäude für Bewirtung', NULL),
  (2081, 'beherbergung_gaststaetten', false, 'direkt', 'Gebäude für Bewirtung', NULL),
  (2083, 'beherbergung_gaststaetten', false, 'direkt', 'Gebäude für Bewirtung', NULL),
  (2090, 'kultureinrichtungen', false, 'naeherung', 'Gebäude für Freizeitaktivitäten', NULL),
  (2091, 'kultureinrichtungen', false, 'naeherung', 'Gebäude für Freizeitaktivitäten', NULL),
  (2092, 'kultureinrichtungen', false, 'direkt',    'Gebäude für Freizeitaktivitäten', NULL),
  (2093, 'sporteinrichtungen',  false, 'naeherung', 'Gebäude für Freizeitaktivitäten', NULL),
  (2094, 'kultureinrichtungen', false, 'naeherung', 'Gebäude für Freizeitaktivitäten', NULL),
  -- Gewerbe, Industrie, Lager
  (2100, 'industrie', false, 'direkt', 'Gebäude für Gewerbe und Industrie', NULL),
  (2110, 'industrie', false, 'direkt', 'Produktionsgebäude', NULL),
  (2111, 'industrie', false, 'direkt', 'Produktionsgebäude', NULL),
  (2120, 'industrie', false, 'direkt', 'Werkstatt', NULL),
  (2130, 'handel', false, 'naeherung', 'Gebäude für Handel und Dienstleistungen', 'Tankstelle'),
  (2140, 'lagergebaeude', false, 'direkt', NULL, NULL),
  (2141, 'lagergebaeude', false, 'naeherung', 'Kühlhaus', 'Kühlhaus: Heizwärme gering, Abwärmepotenzial'),
  (2142, 'lagergebaeude', false, 'unbeheizt', NULL, 'in EnergyMap unbeheizt; KWW-Typ hinterlegt, falls doch gerechnet werden soll'),
  (2143, 'lagergebaeude', false, 'unbeheizt', NULL, 'in EnergyMap unbeheizt; KWW-Typ hinterlegt, falls doch gerechnet werden soll'),
  (2150, 'lagergebaeude', false, 'naeherung', 'Speditionsgebäude', NULL),
  (2160, 'bildungseinrichtungen', false, 'naeherung', 'Gebäude für Forschung und Lehre', NULL),
  (2180, 'buerogebaeude', false, 'naeherung', NULL, 'betriebliche Sozialeinrichtung'),
  (2200, 'industrie', false, 'direkt', 'Sonstiges Gebäude für Gewerbe und Industrie', NULL),
  (2211, NULL, false, 'unbeheizt', NULL, NULL),
  (2310, 'handel', false, 'naeherung', 'Gebäude für Handel und Dienstleistungen', 'mit Wohnen, als Handel'),
  (2320, 'industrie', false, 'naeherung', 'Gebäude für Gewerbe und Industrie', 'mit Wohnen, als Industrie'),
  -- Verkehr
  (2400, 'industrie', false, 'naeherung', NULL, 'Betriebsgebäude Verkehr, werkstattähnlich'),
  (2410, 'industrie', false, 'naeherung', NULL, 'Betriebsgebäude Verkehr, werkstattähnlich'),
  (2411, 'industrie', false, 'naeherung', NULL, 'Straßenmeisterei'),
  (2412, NULL, false, 'unbeheizt', 'Hallen', NULL),
  (2420, 'industrie', false, 'naeherung', 'Gebäude zu öffentlichen Verkehrsmitteln', NULL),
  (2421, 'ezfh', false, 'naeherung', NULL, 'Bahnwärterhaus (Wohnhaus)'),
  (2422, NULL, false, 'unbeheizt', 'Hallen', NULL),
  (2423, NULL, false, 'unbeheizt', NULL, NULL),
  (2424, NULL, false, 'unbeheizt', NULL, NULL),
  (2430, 'buerogebaeude', false, 'naeherung', 'Flughafengebäude', NULL),
  (2431, NULL, false, 'unbeheizt', 'Hallen', NULL),
  (2440, 'industrie', false, 'naeherung', NULL, NULL),
  (2441, NULL, false, 'unbeheizt', NULL, NULL),
  (2442, 'industrie', false, 'naeherung', NULL, NULL),
  (2443, 'industrie', false, 'naeherung', NULL, NULL),
  (2444, NULL, false, 'unbeheizt', NULL, NULL),
  (2460, NULL, false, 'unbeheizt', NULL, NULL),
  (2461, NULL, false, 'unbeheizt', NULL, NULL),
  (2462, NULL, false, 'unbeheizt', NULL, NULL),
  (2463, NULL, false, 'unbeheizt', NULL, NULL),
  (2464, NULL, false, 'unbeheizt', 'Hallen', NULL),
  (2465, NULL, false, 'unbeheizt', NULL, NULL),
  -- Ver- und Entsorgung (Technikgebäude: kein KWW-Typ)
  (2500, NULL, false, 'kein_typ', NULL, 'Technikgebäude, kein KWW-Typ'),
  (2510, NULL, false, 'kein_typ', 'Gebäude zur Wasserversorgung', 'Technikgebäude, kein KWW-Typ'),
  (2511, NULL, false, 'unbeheizt', 'Gebäude zur Wasserversorgung', NULL),
  (2512, NULL, false, 'kein_typ', 'Gebäude zur Wasserversorgung', 'Technikgebäude, kein KWW-Typ'),
  (2513, NULL, false, 'unbeheizt', 'Gebäude zur Wasserversorgung', NULL),
  (2520, NULL, false, 'kein_typ', 'Gebäude zur Energieversorgung', 'Technikgebäude, kein KWW-Typ'),
  (2521, NULL, false, 'unbeheizt', 'Gebäude zur Energieversorgung', NULL),
  (2522, NULL, false, 'unbeheizt', 'Gebäude zur Energieversorgung', NULL),
  (2523, NULL, false, 'unbeheizt', 'Gebäude zur Energieversorgung', NULL),
  (2540, 'buerogebaeude', false, 'naeherung', NULL, 'Fernmeldewesen'),
  (2560, NULL, false, 'unbeheizt', NULL, NULL),
  (2570, NULL, false, 'kein_typ', 'Gebäude zur Gasversorgung', 'Technikgebäude, kein KWW-Typ'),
  (2571, NULL, false, 'unbeheizt', 'Gaswerk', NULL),
  (2580, NULL, false, 'unbeheizt', 'Gebäude zur Energieversorgung', 'Heizwerk'),
  (2591, NULL, false, 'unbeheizt', NULL, NULL),
  (2600, NULL, false, 'kein_typ', NULL, 'Technikgebäude, kein KWW-Typ'),
  (2610, NULL, false, 'unbeheizt', NULL, NULL),
  (2611, NULL, false, 'unbeheizt', NULL, NULL),
  (2612, NULL, false, 'unbeheizt', NULL, NULL),
  (2620, NULL, false, 'unbeheizt', NULL, NULL),
  (2621, NULL, false, 'unbeheizt', NULL, NULL),
  (2622, NULL, false, 'kein_typ', 'Gebäude zur Energieversorgung', 'Müllverbrennung, kein KWW-Typ'),
  -- Land- und Forstwirtschaft, Gartenbau
  (2700, NULL, false, 'unbeheizt', 'Gebäude für Land- und Forstwirtschaft', NULL),
  (2720, 'industrie', false, 'naeherung', 'Land- und forstwirtschaftliches Betriebsgebäude', NULL),
  (2721, NULL, false, 'unbeheizt', NULL, NULL),
  (2723, NULL, false, 'unbeheizt', NULL, NULL),
  (2724, NULL, false, 'unbeheizt', NULL, NULL),
  (2726, NULL, false, 'unbeheizt', NULL, NULL),
  (2727, NULL, false, 'unbeheizt', 'Stall für Tiergroßhaltung', NULL),
  (2740, NULL, false, 'kein_typ', 'Gewächshaus', 'Gewächshaus: Wärmebedarf hängt an Kultur, kein KWW-Typ'),
  (2741, NULL, false, 'kein_typ', 'Gewächshaus', 'Gewächshaus: Wärmebedarf hängt an Kultur, kein KWW-Typ'),
  (2742, NULL, false, 'kein_typ', 'Gewächshaus', 'Gewächshaus: Wärmebedarf hängt an Kultur, kein KWW-Typ'),
  -- Öffentliche Zwecke
  (3000, 'buerogebaeude', false, 'naeherung', 'Büroähnliche Betriebe', NULL),
  (3010, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (3011, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (3012, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (3013, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (3014, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (3015, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (3016, 'buerogebaeude', false, 'direkt', 'Büroähnliche Betriebe', NULL),
  (3020, 'bildungseinrichtungen', false, 'direkt', 'Gebäude für Forschung und Lehre', NULL),
  (3021, 'bildungseinrichtungen', false, 'direkt', 'Gebäude für Bildungszwecke', NULL),
  (3022, 'bildungseinrichtungen', false, 'direkt', 'Gebäude für Bildungszwecke', NULL),
  (3023, 'bildungseinrichtungen', false, 'direkt', 'Gebäude für Forschung und Lehre', NULL),
  (3024, 'bildungseinrichtungen', false, 'naeherung', 'Gebäude für Forschung und Lehre', NULL),
  (3030, 'kultureinrichtungen', false, 'direkt', 'Gebäude für Freizeitaktivitäten', NULL),
  (3031, 'kultureinrichtungen', false, 'direkt', 'Gebäude für Freizeitaktivitäten', NULL),
  (3032, 'kultureinrichtungen', false, 'direkt', 'Gebäude für Freizeitaktivitäten', NULL),
  (3033, 'kultureinrichtungen', false, 'direkt', 'Gebäude für Freizeitaktivitäten', NULL),
  (3034, 'kultureinrichtungen', false, 'direkt', 'Gebäude für Freizeitaktivitäten', NULL),
  (3035, 'buerogebaeude', false, 'naeherung', 'Büroähnliche Betriebe', 'Rundfunk, Fernsehen'),
  (3036, 'kultureinrichtungen', false, 'direkt', 'Gebäude für Freizeitaktivitäten', NULL),
  (3037, 'kultureinrichtungen', false, 'direkt', 'Gebäude für Freizeitaktivitäten', 'Bibliothek'),
  (3040, 'kultureinrichtungen', false, 'naeherung', 'Religiöse Einrichtungen', NULL),
  (3041, 'kultureinrichtungen', false, 'naeherung', 'Religiöse Einrichtungen', NULL),
  (3042, 'kultureinrichtungen', false, 'naeherung', 'Religiöse Einrichtungen', NULL),
  (3043, 'kultureinrichtungen', false, 'naeherung', 'Religiöse Einrichtungen', NULL),
  (3044, 'kultureinrichtungen', false, 'naeherung', 'Religiöse Einrichtungen', 'Gemeindehaus'),
  (3045, 'kultureinrichtungen', false, 'naeherung', 'Religiöse Einrichtungen', NULL),
  (3046, 'kultureinrichtungen', false, 'naeherung', 'Religiöse Einrichtungen', NULL),
  (3050, 'krankenhaeuser', false, 'direkt', 'Gebäude für Gesundheitswesen', NULL),
  (3051, 'krankenhaeuser', false, 'direkt', 'Gebäude für Gesundheitswesen', NULL),
  (3052, 'krankenhaeuser', false, 'naeherung', 'Gebäude für Gesundheitswesen', 'Pflegeanstalt'),
  (3060, 'bildungseinrichtungen', false, 'naeherung', NULL, 'Gebäude für soziale Zwecke'),
  (3061, 'kultureinrichtungen', false, 'naeherung', 'Gebäude für Freizeitaktivitäten', 'Jugendfreizeitheim'),
  (3062, 'kultureinrichtungen', false, 'naeherung', 'Gebäude für Freizeitaktivitäten', NULL),
  (3063, 'kultureinrichtungen', false, 'naeherung', 'Gebäude für Freizeitaktivitäten', NULL),
  (3064, 'beherbergung_gaststaetten', false, 'direkt', 'Wohnheime', NULL),
  (3065, 'bildungseinrichtungen', false, 'direkt', 'Gebäude für Bildungszwecke', NULL),
  (3070, 'buerogebaeude', false, 'naeherung', 'Büroähnliche Betriebe', NULL),
  (3071, 'buerogebaeude', false, 'naeherung', 'Büroähnliche Betriebe', NULL),
  (3072, 'industrie', false, 'naeherung', 'Feuerwehr', 'Feuerwehr: Tab 56 ordnet IWU-Typ 8 (Werkstatt) zu'),
  (3073, 'beherbergung_gaststaetten', false, 'naeherung', 'Kaserne', NULL),
  (3074, NULL, false, 'unbeheizt', NULL, NULL),
  (3075, 'beherbergung_gaststaetten', false, 'naeherung', 'Wohnheime', 'Justizvollzugsanstalt'),
  (3080, NULL, false, 'unbeheizt', NULL, NULL),
  (3081, NULL, false, 'unbeheizt', NULL, NULL),
  (3082, NULL, false, 'kein_typ', 'Krematorium', 'Krematorium, Prozesswärme'),
  (3090, 'buerogebaeude', false, 'naeherung', 'Empfangsgebäude', NULL),
  (3091, 'handel', false, 'naeherung', 'Gebäude zu öffentlichen Verkehrsmitteln', 'Bahnhof'),
  (3092, 'handel', false, 'naeherung', 'Flughafengebäude', 'Flughafen'),
  (3094, 'handel', false, 'naeherung', 'Gebäude zu öffentlichen Verkehrsmitteln', NULL),
  (3095, 'handel', false, 'naeherung', 'Gebäude zu öffentlichen Verkehrsmitteln', NULL),
  (3097, 'handel', false, 'naeherung', 'Gebäude zu öffentlichen Verkehrsmitteln', NULL),
  (3100, 'beherbergung_gaststaetten', false, 'naeherung', 'Gebäude für öffentliche Zwecke mit Wohnen', NULL),
  -- Erholung, Sport, Bäder, Zoo, Botanik
  (3200, 'beherbergung_gaststaetten', false, 'naeherung', 'Gebäude für Erholungszwecke', NULL),
  (3210, 'sporteinrichtungen', false, 'direkt', NULL, NULL),
  (3211, 'sporteinrichtungen', false, 'direkt', 'Hallen', 'Sport-, Turnhalle'),
  (3212, 'sporteinrichtungen', false, 'direkt', NULL, 'Gebäude zum Sportplatz'),
  (3220, 'schwimmbaeder', false, 'direkt', 'Badegebäude, Hallenbad', NULL),
  (3221, 'schwimmbaeder', false, 'direkt', 'Badegebäude, Hallenbad', NULL),
  (3222, NULL, false, 'unbeheizt', NULL, 'Gebäude im Freibad'),
  (3240, 'beherbergung_gaststaetten', false, 'naeherung', 'Gebäude für Erholungszwecke', 'Kurbetrieb'),
  (3241, 'schwimmbaeder', false, 'direkt', 'Badegebäude, Hallenbad', 'medizinisches Bad'),
  (3260, 'kultureinrichtungen', false, 'naeherung', NULL, 'Zoo'),
  (3261, 'kultureinrichtungen', false, 'naeherung', 'Empfangsgebäude', 'Zoo'),
  (3262, 'kultureinrichtungen', false, 'naeherung', 'Aquarium', NULL),
  (3263, 'kultureinrichtungen', false, 'naeherung', NULL, 'Tierschauhaus, Bedarf vermutlich höher'),
  (3264, 'kultureinrichtungen', false, 'naeherung', NULL, 'Stall im Zoo'),
  (3270, 'kultureinrichtungen', false, 'naeherung', 'Gebäude im botanischen Garten', NULL),
  (3271, 'kultureinrichtungen', false, 'naeherung', 'Empfangsgebäude', NULL),
  (3272, NULL, false, 'kein_typ', 'Gewächshaus', 'Gewächshaus (Botanik), kein KWW-Typ'),
  (3273, NULL, false, 'kein_typ', 'Gewächshaus', 'Pflanzenschauhaus, kein KWW-Typ'),
  (3281, NULL, false, 'unbeheizt', NULL, NULL),
  (9998, NULL, false, 'unbeheizt', NULL, 'nach Quellenlage nicht zu spezifizieren')
),
ph AS (  -- Präfix -> voller Funktionsname aus Tab 56, muss eindeutig sein
  SELECT m.gfk, p.function_label, count(*) OVER (PARTITION BY m.gfk) AS n
  FROM m JOIN core_bldg.kww_process_heat p ON p.function_label LIKE m.ph || '%'
)
INSERT INTO core_bldg.kww_gfk_mapping
SELECT m.gfk,
       coalesce(o.name, 'unbekannt (' || m.gfk || ')'),
       coalesce(d.heated, true) AND m.quality <> 'unbeheizt',
       m.kww_type, m.split, m.quality, ph.function_label, m.remark
FROM m
LEFT JOIN zoning.alkis_function_official o USING (gfk)
LEFT JOIN emc.def_bldg_function_gfk d USING (gfk)
LEFT JOIN ph USING (gfk);

-- Prüfungen: Präfixe eindeutig, alle Berliner GFK zugeordnet, unbeheizt konsistent mit EnergyMap
DO $$
DECLARE r record;
BEGIN
  IF EXISTS (SELECT 1 FROM core_bldg.kww_gfk_mapping GROUP BY gfk HAVING count(*) > 1) THEN
    RAISE EXCEPTION 'Prozesswärme-Präfix nicht eindeutig';
  END IF;
  FOR r IN SELECT DISTINCT b.gfk FROM emc.building b
           LEFT JOIN core_bldg.kww_gfk_mapping k USING (gfk)
           WHERE b.gfk IS NOT NULL AND k.gfk IS NULL LOOP
    RAISE EXCEPTION 'GFK % aus emc.building fehlt in der Zuordnung', r.gfk;
  END LOOP;
  FOR r IN SELECT k.gfk FROM core_bldg.kww_gfk_mapping k JOIN emc.def_bldg_function_gfk d USING (gfk)
           WHERE d.heated = false AND k.heated LOOP
    RAISE EXCEPTION 'GFK % ist in EnergyMap unbeheizt, hier beheizt', r.gfk;
  END LOOP;
  FOR r IN SELECT k.gfk FROM core_bldg.kww_gfk_mapping k JOIN emc.def_bldg_function_gfk d USING (gfk)
           WHERE coalesce(d.heated, true) AND NOT k.heated LOOP
    RAISE NOTICE 'GFK % hier unbeheizt, in EnergyMap beheizt/offen', r.gfk;
  END LOOP;
END $$;

COMMIT;
