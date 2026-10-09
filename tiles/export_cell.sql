-- Export einer 1-km-Zelle für die Tile-Pipeline (nur lesend).
--
-- Aufruf: psql -v x0=.. -v y0=.. -v size=1000 -v region='Neukölln' -f export_cell.sql
--   x0/y0  linke untere Ecke der Zelle in EPSG:25833
--   region Bezirksname aus emc.district, oder '*' für ganz Berlin (emc.city)
--
-- Die Datenbank liefert nur KANDIDATEN mit Bewertung. Welcher LoD2-Körper zu welchem 2D-Gebäude gehört,
-- entscheidet assign.py einmal global über alle Zellen. Grund: ein Körper am Zellrand kann von Gebäuden
-- aus zwei Zellen beansprucht werden; zellweise entschieden würde er doppelt vergeben.
--
-- Ausgabe (stdout), CSV-Blöcke mit Kopfzeile, getrennt durch Markerzeilen:
--   #BUILDINGS  2D-Gebäude, deren Grundriss-Mittelpunkt in Zelle UND Region liegt (jedes genau einmal)
--   #BRIDGE     Zeilen der Verknüpfungstabelle für diese Gebäude
--   #OVERLAP    für Gebäude ohne Brückenzeile: Überlappungsfläche je LoD2-Körper (über dessen Bodenflächen)
--   #BODIES     Dach/Wand-Flächen aller Körper aus BRIDGE und OVERLAP
--   #END        Prüfmarke: fehlt sie, ist die Datei unvollständig
SET default_transaction_read_only = on;
SET max_parallel_workers_per_gather = 0;

\echo '#BUILDINGS'
COPY (
    WITH cell AS (SELECT ST_MakeEnvelope(:x0, :y0, :x0 + :size, :y0 + :size, 25833) AS g),
    region AS (
        SELECT geom FROM emc.district WHERE name = :'region'
        UNION ALL SELECT geom FROM emc.city WHERE :'region' = '*' AND name = 'Berlin'
    ),
    b2d AS (
        SELECT b.uuid, b.geom, ST_PointOnSurface(b.geom) AS p
        FROM emc.building b, cell
        WHERE b.geom && cell.g
    )
    SELECT b.uuid AS bldg_uuid,
           ST_AsGeoJSON(b.geom, 2) AS footprint,
           -- vereinfachte Grundrisse für die groben Detailstufen
           ST_AsGeoJSON(ST_SimplifyPreserveTopology(b.geom, 1.0), 2) AS footprint_simple,
           ST_AsGeoJSON(ST_SimplifyPreserveTopology(b.geom, 2.5), 1) AS footprint_overview,
           round(ST_Area(b.geom)::numeric, 1) AS area,
           GREATEST(3, LEAST(100, COALESCE(
               pb."Gross volume" / NULLIF(pb."Footprint area", 0),
               pb."Storey number" * COALESCE(pb."Average Storey Height", 3),
               9))) AS est_height,
           pb."Heated area" AS heated_area,
           pb."PrimaryUsageZoneType" AS usage,
           pb."Year of construction" AS year,
           e.cons_c2r2 AS heat_kwh,
           e.spec_cons_c2r2 AS heat_spec,
           e.street_name, e.house_number
    FROM b2d b
    CROSS JOIN cell
    LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
    LEFT JOIN emp.emblive e ON e.uuid = b.uuid
    WHERE ST_Intersects(b.p, cell.g)
      AND EXISTS (SELECT 1 FROM region r WHERE ST_Intersects(b.p, r.geom))
) TO STDOUT WITH (FORMAT csv, HEADER);

\echo '#BRIDGE'
COPY (
    WITH cell AS (SELECT ST_MakeEnvelope(:x0, :y0, :x0 + :size, :y0 + :size, 25833) AS g),
    region AS (
        SELECT geom FROM emc.district WHERE name = :'region'
        UNION ALL SELECT geom FROM emc.city WHERE :'region' = '*' AND name = 'Berlin'
    ),
    core AS (
        SELECT b.uuid FROM emc.building b, cell
        WHERE b.geom && cell.g AND ST_Intersects(ST_PointOnSurface(b.geom), cell.g)
          AND EXISTS (SELECT 1 FROM region r WHERE ST_Intersects(ST_PointOnSurface(b.geom), r.geom))
    )
    SELECT r.bldg_uuid, r.citygml_bldg_id AS body, r.match_type, r.relative_intersection
    FROM core JOIN emc.relation_bldg_citygml_v2 r ON r.bldg_uuid = core.uuid
    WHERE r.citygml_bldg_id IS NOT NULL
) TO STDOUT WITH (FORMAT csv, HEADER);

\echo '#OVERLAP'
COPY (
    WITH cell AS (SELECT ST_MakeEnvelope(:x0, :y0, :x0 + :size, :y0 + :size, 25833) AS g),
    region AS (
        SELECT geom FROM emc.district WHERE name = :'region'
        UNION ALL SELECT geom FROM emc.city WHERE :'region' = '*' AND name = 'Berlin'
    ),
    unbridged AS (
        SELECT b.uuid, ST_MakeValid(b.geom) AS geom FROM emc.building b, cell
        WHERE b.geom && cell.g AND ST_Intersects(ST_PointOnSurface(b.geom), cell.g)
          AND EXISTS (SELECT 1 FROM region r WHERE ST_Intersects(ST_PointOnSurface(b.geom), r.geom))
          AND NOT EXISTS (
              SELECT 1 FROM emc.relation_bldg_citygml_v2 rel
              JOIN citydb.thematic_surface ts ON ts.building_id = rel.citygml_bldg_id
              WHERE rel.bldg_uuid = b.uuid AND ts.objectclass_id IN (33, 34))
    )
    SELECT u.uuid AS bldg_uuid, ts.building_id AS body,
           round(SUM(ST_Area(ST_Intersection(ST_MakeValid(ST_Force2D(sg.geometry)), u.geom)))::numeric, 2) AS overlap_area
    FROM unbridged u
    JOIN citydb.surface_geometry sg ON sg.geometry && u.geom
    JOIN citydb.thematic_surface ts ON ts.lod2_multi_surface_id = sg.root_id AND ts.objectclass_id = 35
    WHERE ST_Intersects(ST_Force2D(sg.geometry), u.geom)
    GROUP BY u.uuid, ts.building_id
    HAVING SUM(ST_Area(ST_Intersection(ST_MakeValid(ST_Force2D(sg.geometry)), u.geom))) > 0.5
) TO STDOUT WITH (FORMAT csv, HEADER);

\echo '#BODIES'
COPY (
    WITH cell AS (SELECT ST_MakeEnvelope(:x0, :y0, :x0 + :size, :y0 + :size, 25833) AS g),
    region AS (
        SELECT geom FROM emc.district WHERE name = :'region'
        UNION ALL SELECT geom FROM emc.city WHERE :'region' = '*' AND name = 'Berlin'
    ),
    core AS (
        SELECT b.uuid, b.geom FROM emc.building b, cell
        WHERE b.geom && cell.g AND ST_Intersects(ST_PointOnSurface(b.geom), cell.g)
          AND EXISTS (SELECT 1 FROM region r WHERE ST_Intersects(ST_PointOnSurface(b.geom), r.geom))
    ),
    unbridged AS (
        SELECT c.uuid, c.geom FROM core c
        WHERE NOT EXISTS (
            SELECT 1 FROM emc.relation_bldg_citygml_v2 rel
            JOIN citydb.thematic_surface ts ON ts.building_id = rel.citygml_bldg_id
            WHERE rel.bldg_uuid = c.uuid AND ts.objectclass_id IN (33, 34))
    ),
    -- dieselben Körper wie in BRIDGE und OVERLAP
    bodies AS (
        SELECT r.citygml_bldg_id AS body FROM core JOIN emc.relation_bldg_citygml_v2 r ON r.bldg_uuid = core.uuid
        UNION
        SELECT ts.building_id FROM unbridged u
        JOIN citydb.surface_geometry sg ON sg.geometry && u.geom
        JOIN citydb.thematic_surface ts ON ts.lod2_multi_surface_id = sg.root_id AND ts.objectclass_id = 35
        WHERE ST_Intersects(ST_Force2D(sg.geometry), u.geom)
    )
    -- nur Dach (33) und Wand (34); Böden sieht man von oben nie
    SELECT ts.building_id AS body, ts.objectclass_id AS cls, ST_AsGeoJSON(sg.geometry, 2) AS geom
    FROM bodies
    JOIN citydb.thematic_surface ts ON ts.building_id = bodies.body AND ts.objectclass_id IN (33, 34)
    JOIN citydb.surface_geometry sg ON sg.root_id = ts.lod2_multi_surface_id
    WHERE sg.geometry IS NOT NULL
) TO STDOUT WITH (FORMAT csv, HEADER);

\echo '#END'
