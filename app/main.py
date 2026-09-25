import json
import os

from dotenv import load_dotenv

# Must run before any local module import: .agent.chat reads OLLAMA_BASE_URL/OLLAMA_CLIENT_CERT/
# OLLAMA_CLIENT_KEY at import time, so loading .env any later leaves it permanently on the
# hardcoded fallback values for the process's whole lifetime instead of the real config.
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from geoalchemy2.functions import ST_X, ST_Y
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .agent.chat import run_chat
from .database import get_db
from .models import Location
from .schemas import LocationOut
from .zoning_api import router as zoning_router

MAX_BUILDINGS_PER_REQUEST = 5000
MAX_TREES_PER_REQUEST = 8000
MIN_CROWN_RADIUS_M = 0.5
MAX_CHILLERS_PER_REQUEST = 5437

app = FastAPI(title="Enpageo Map API")

cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


app.include_router(zoning_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/locations", response_model=list[LocationOut])
def list_locations(db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            Location.id,
            Location.name,
            Location.category,
            Location.description,
            ST_Y(Location.geom).label("latitude"),
            ST_X(Location.geom).label("longitude"),
        )
    ).all()
    return [
        LocationOut(
            id=row.id,
            name=row.name,
            category=row.category,
            description=row.description,
            latitude=row.latitude,
            longitude=row.longitude,
        )
        for row in rows
    ]


# emc.district = the 12 official Berlin Bezirke; emc.city (414 Berlin/Brandenburg
# municipalities) filtered to "Berlin" gives the single city outline. Both are small,
# static datasets, so the whole FeatureCollection is returned in one go (no bbox filtering).
BOUNDARY_QUERIES = {
    "bezirke": """
        SELECT dist_id AS id, name, ST_AsGeoJSON(ST_Transform(geom, 4326)) AS geom
        FROM emc.district ORDER BY name
    """,
    "stadtgrenze": """
        SELECT id, name, ST_AsGeoJSON(ST_Transform(geom, 4326)) AS geom
        FROM emc.city WHERE name = 'Berlin'
    """,
}


@app.get("/api/boundaries")
def get_boundaries(type: str = Query(...), db: Session = Depends(get_db)):
    query = BOUNDARY_QUERIES.get(type)
    if not query:
        raise HTTPException(status_code=400, detail=f"unknown boundary type '{type}', expected one of {list(BOUNDARY_QUERIES)}")
    rows = db.execute(text(query)).all()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": json.loads(row.geom),
                "properties": {"id": row.id, "name": row.name},
            }
            for row in rows
        ],
    }


@app.get("/api/buildings")
def list_buildings(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat in WGS84"),
    month: int = Query(7, ge=1, le=12, description="1-12, used to pick the cooling_demand_MM column"),
    db: Session = Depends(get_db),
):
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be 'min_lon,min_lat,max_lon,max_lat'")

    # core_bldg.calib_monthly (a per-building simulation result, but 36 GB for 221 of 222
    # climate/refurbishment combinations nothing here ever queried) was dropped. Cooling is now
    # a generic per-usage-type placeholder from core_bldg.usage_cooling_profile — see that
    # table's comment. `month` still picks a real column, just pct_MM (0-100) instead of a
    # simulated kWh figure; the FastAPI Query(..., ge=1, le=12) bound above makes the
    # f-string safe (only "pct_01".."pct_12" can ever appear here).
    pct_col = f"pct_{month:02d}"

    rows = db.execute(
        text(
            f"""
            SELECT
                b.uuid AS bldg_uuid,
                ST_AsGeoJSON(ST_Transform(b.geom, 4326)) AS geom,
                COALESCE(pb."PrimaryUsageZoneArea", pb."Heated area", 0)
                    * COALESCE(ucp.specific_annual_cooling_kwh_m2, 20)
                    * COALESCE(ucp."{pct_col}", 30) / 100 AS cooling_demand,
                pb."PrimaryUsageZoneType" AS usage_zone_type
            FROM emc.building b
            LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
            LEFT JOIN core_bldg.usage_cooling_profile ucp ON ucp.usage_zone_type = pb."PrimaryUsageZoneType"
            WHERE b.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
            LIMIT :limit
            """
        ),
        {
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
            "limit": MAX_BUILDINGS_PER_REQUEST,
        },
    ).all()

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": json.loads(row.geom),
                "properties": {
                    "bldg_uuid": row.bldg_uuid,
                    "cooling_demand": row.cooling_demand,
                    "usage_zone_type": row.usage_zone_type,
                },
            }
            for row in rows
        ],
    }


# CityGML objectclass_id -> surface kind. Same numbering the zoning tool's endpoint uses
# (app/zoning_api.py): 33 is the roof and 34 the wall here, not the other way round.
SURFACE_KIND_3D = {33: "roof", 34: "wall", 35: "ground"}
# 800, paired with the frontend's zoom>=17 gate: a Berlin-wide grid sample at that viewport
# size averaged ~223 buildings per view (max ~1020), so 800 leaves ~0% missing to the cap
# while staying well under a second end-to-end (measured ~0.9s / ~680KB for a real ~260-
# building view). 300 was an untested guess that left up to 19% of buildings missing at
# zoom 17 purely from being cut off, not from any real data gap.
MAX_BUILDINGS_3D_PER_REQUEST = 800


def _shift_z(coords, base: float) -> None:
    """Subtract a shared base elevation from every z, in place (GeoJSON coordinate tree)."""
    if coords and isinstance(coords[0], (int, float)):
        if len(coords) > 2:
            coords[2] = round(coords[2] - base, 3)
        return
    for c in coords:
        _shift_z(c, base)


def _extrude_footprint(geojson_geom: dict, height: float) -> list[dict]:
    """Build a flat box (ground/roof/walls) from a 2D footprint for buildings with no real
    LoD2 model -- clearly worse than an actual solid, but better than the building just being
    absent; the frontend renders these in a muted colour so they aren't mistaken for real
    geometry (see zoning_api's own "noLod2" fallback for the same idea on a single building)."""
    polys = geojson_geom["coordinates"]
    if geojson_geom["type"] == "Polygon":
        polys = [polys]
    surfaces = []
    for poly in polys:
        ring = poly[0]  # exterior ring only; courtyards/holes aren't worth modelling for a box
        surfaces.append({"kind": "ground", "rings": [[[p[0], p[1], 0] for p in ring]]})
        surfaces.append({"kind": "roof", "rings": [[[p[0], p[1], height] for p in ring]]})
        for i in range(len(ring) - 1):
            p0, p1 = ring[i], ring[i + 1]
            wall = [
                [p0[0], p0[1], 0],
                [p1[0], p1[1], 0],
                [p1[0], p1[1], height],
                [p0[0], p0[1], height],
                [p0[0], p0[1], 0],
            ]
            surfaces.append({"kind": "wall", "rings": [wall]})
    return surfaces


@app.get("/api/buildings3d")
def list_buildings_3d(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat in WGS84"),
    db: Session = Depends(get_db),
):
    """Real LoD2 wall/roof/ground surfaces for buildings in the viewport, from citydb.

    Whole-city equivalent of the zoning tool's per-building /api/zoning/{uuid}/lod2 (see
    that endpoint's docstring for why an extruded footprint is the wrong drawing for a
    pitched roof). Bridged via emc.relation_bldg_citygml_v2 rather than the zoning
    schema's ground-surface-matched bridge -- Berlin-wide, but ~7% less accurate per the
    zoning tool's own finding; fine for a visual map layer, not for measurement.

    Heights come out of CityGML as metres above sea level; each building is shifted by
    its own lowest point, same as the single-building endpoint -- there's no terrain in
    the Mapbox custom layer, so every building's own ground has to sit at the flat map's
    z=0 or it floats.

    Buildings the bridge table has no candidate for are NOT necessarily missing from
    citydb -- a spot-check found 687 of 691 such buildings (99.4%) actually have real
    citydb geometry overlapping their footprint; the bridge table itself is just
    incomplete. So before falling back to an extruded box, a second query spatially
    matches those buildings directly against citydb ground surfaces (below, "unmatched"/
    "overlap_candidates"/etc). Only what's still unmatched after THAT gets a box.
    """
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be 'min_lon,min_lat,max_lon,max_lat'")

    rows = db.execute(
        text(
            """
            WITH candidates AS (
                -- relation_bldg_citygml_v2 has ~5.4 candidate 3D matches per building on
                -- average (a 2D EnergyMap building can overlap several CityGML solids).
                SELECT b.uuid AS bldg_uuid, rel.citygml_bldg_id, rel.match_type, rel.relative_intersection
                FROM emc.building b
                JOIN emc.relation_bldg_citygml_v2 rel ON rel.bldg_uuid = b.uuid
                WHERE b.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
            ),
            -- An exact "1:1" candidate is kept alone. But "One-2D:n-3D" means the 2D
            -- building's real footprint is split into n separate CityGML parts (e.g. wings
            -- of one address that were LoD2-modelled as several building bodies) -- taking
            -- only the single best-overlap part, as an earlier version of this query did,
            -- silently dropped the rest: found via a building whose 4 real parts summed to
            -- its exact 2D footprint area, but only the 42.7%-overlap part ever rendered.
            -- So for a building with no "1:1" match, every non-"1:1" candidate is kept and
            -- their surfaces all get merged under that one bldg_uuid downstream in Python.
            matched_raw AS (
                SELECT bldg_uuid, citygml_bldg_id, relative_intersection FROM candidates WHERE match_type = '1:1'
                UNION ALL
                SELECT c.bldg_uuid, c.citygml_bldg_id, c.relative_intersection
                FROM candidates c
                WHERE c.match_type != '1:1'
                  AND NOT EXISTS (
                      SELECT 1 FROM candidates c2
                      WHERE c2.bldg_uuid = c.bldg_uuid AND c2.match_type = '1:1'
                  )
            ),
            -- Keeping every part for a "One-2D:n-3D" building (above) means the SAME part
            -- can end up claimed by two different neighbouring 2D buildings (Berlin-wide,
            -- ~1991 CityGML parts do) -- give it to whichever one overlaps it best and drop
            -- it for the other, rather than draw that one part twice at two addresses.
            matched AS (
                SELECT bldg_uuid, citygml_bldg_id
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY citygml_bldg_id ORDER BY COALESCE(relative_intersection, 0) DESC
                    ) AS rn
                    FROM matched_raw
                ) ranked
                WHERE rn = 1
            ),
            -- LIMIT has to sit here, after confirming real LoD2 surfaces exist -- capping
            -- the raw candidate list instead let empty buildings (no citydb match, or a
            -- match with no roof/wall geometry) use up slots that real buildings needed.
            capped AS (
                SELECT m.bldg_uuid, m.citygml_bldg_id
                FROM matched m
                WHERE EXISTS (
                    SELECT 1 FROM citydb.thematic_surface ts
                    WHERE ts.building_id = m.citygml_bldg_id AND ts.objectclass_id = ANY(:cls)
                )
                LIMIT :limit
            )
            SELECT c.bldg_uuid, ts.objectclass_id AS cls,
                   ST_ZMin(sg.geometry) AS zmin,
                   ST_AsGeoJSON(ST_Transform(sg.geometry, 4326), 7) AS geom
            FROM capped c
            JOIN citydb.thematic_surface ts ON ts.building_id = c.citygml_bldg_id
            JOIN citydb.surface_geometry sg ON sg.root_id = ts.lod2_multi_surface_id
            WHERE ts.objectclass_id = ANY(:cls) AND sg.geometry IS NOT NULL
            """
        ),
        {
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
            "limit": MAX_BUILDINGS_3D_PER_REQUEST,
            "cls": list(SURFACE_KIND_3D),
        },
    ).all()

    # Mapbox custom layers draw on a flat plane (no real terrain), so z=0 has to mean
    # "this building's own ground" for every building, not one shared viewport minimum --
    # a shared base left buildings on higher ground floating above the flat map, since
    # their own zmin was above that minimum. Relative elevation between buildings on real
    # terrain isn't recoverable on a flat map anyway, so per-building is both simpler and
    # matches what the base extrusion layer already assumes.
    rows_by_building: dict[str, list] = {}
    for r in rows:
        rows_by_building.setdefault(r.bldg_uuid, []).append(r)

    by_building: dict[str, dict] = {}
    lon_sum = lat_sum = 0.0
    n = 0
    for bldg_uuid, brows in rows_by_building.items():
        base = min(float(r.zmin) for r in brows)
        surfaces = []
        for r in brows:
            g = json.loads(r.geom)
            _shift_z(g["coordinates"], base)
            surfaces.append({"kind": SURFACE_KIND_3D[r.cls], "rings": g["coordinates"]})
        by_building[bldg_uuid] = {"surfaces": surfaces, "estimated": False}
        # centroid of the first surface's first point, good enough for a viewport-sized origin
        pt = surfaces[0]["rings"][0][0]
        lon_sum += pt[0]
        lat_sum += pt[1]
        n += 1

    # The bridge table is incomplete (see docstring), so buildings it has no candidate
    # for get a second chance: spatially match them directly against citydb ground
    # surfaces before assuming there's really no geometry for them.
    remaining = MAX_BUILDINGS_3D_PER_REQUEST - len(by_building)
    if remaining > 0:
        spatial_rows = db.execute(
            text(
                """
                WITH unmatched AS (
                    SELECT b.uuid, b.geom
                    FROM emc.building b
                    WHERE b.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
                      AND NOT (b.uuid = ANY(:already_found))
                      AND NOT EXISTS (
                          SELECT 1 FROM emc.relation_bldg_citygml_v2 rel
                          JOIN citydb.thematic_surface ts ON ts.building_id = rel.citygml_bldg_id
                          WHERE rel.bldg_uuid = b.uuid AND ts.objectclass_id = ANY(:cls)
                      )
                ),
                -- Ground surfaces are the most reliable footprint proxy for scoring overlap
                -- (walls project to near-zero-area lines in 2D; roofs can overhang past the
                -- footprint). && before ST_Intersects lets the geometry's GIST index narrow
                -- candidates before the exact overlap area gets computed.
                -- Not "overlaps" -- that's a reserved SQL keyword (the temporal predicate
                -- "x OVERLAPS y") and breaks the parser when used unquoted as a CTE name.
                overlap_candidates AS (
                    SELECT u.uuid AS bldg_uuid, ts.building_id AS citygml_bldg_id,
                           ST_Area(ST_Intersection(ST_Force2D(sg.geometry), u.geom))
                               / GREATEST(ST_Area(u.geom), 1) AS overlap_frac
                    FROM unmatched u
                    JOIN citydb.surface_geometry sg ON sg.geometry && u.geom
                    JOIN citydb.thematic_surface ts ON ts.lod2_multi_surface_id = sg.root_id AND ts.objectclass_id = 35
                    WHERE ST_Intersects(ST_Force2D(sg.geometry), u.geom)
                ),
                -- best match per 2D building, then (as with the bridge table above) resolved
                -- the other way too so two neighbours can't both claim the same citygml body
                best_per_building AS (
                    SELECT bldg_uuid, citygml_bldg_id, overlap_frac
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (PARTITION BY bldg_uuid ORDER BY overlap_frac DESC) AS rn
                        FROM overlap_candidates
                    ) r
                    WHERE rn = 1 AND overlap_frac >= 0.15
                ),
                dedup AS (
                    SELECT bldg_uuid, citygml_bldg_id
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (PARTITION BY citygml_bldg_id ORDER BY overlap_frac DESC) AS rn
                        FROM best_per_building
                    ) r
                    WHERE rn = 1
                ),
                capped AS (
                    SELECT bldg_uuid, citygml_bldg_id FROM dedup LIMIT :limit
                )
                SELECT c.bldg_uuid, ts.objectclass_id AS cls,
                       ST_ZMin(sg.geometry) AS zmin,
                       ST_AsGeoJSON(ST_Transform(sg.geometry, 4326), 7) AS geom
                FROM capped c
                JOIN citydb.thematic_surface ts ON ts.building_id = c.citygml_bldg_id
                JOIN citydb.surface_geometry sg ON sg.root_id = ts.lod2_multi_surface_id
                WHERE ts.objectclass_id = ANY(:cls) AND sg.geometry IS NOT NULL
                """
            ),
            {
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
                "cls": list(SURFACE_KIND_3D),
                "already_found": list(by_building.keys()),
                "limit": remaining,
            },
        ).all()

        spatial_rows_by_building: dict[str, list] = {}
        for r in spatial_rows:
            spatial_rows_by_building.setdefault(r.bldg_uuid, []).append(r)
        for bldg_uuid, brows in spatial_rows_by_building.items():
            base = min(float(r.zmin) for r in brows)
            surfaces = []
            for r in brows:
                g = json.loads(r.geom)
                _shift_z(g["coordinates"], base)
                surfaces.append({"kind": SURFACE_KIND_3D[r.cls], "rings": g["coordinates"]})
            by_building[bldg_uuid] = {"surfaces": surfaces, "estimated": False}
            pt = surfaces[0]["rings"][0][0]
            lon_sum += pt[0]
            lat_sum += pt[1]
            n += 1

    # Whatever's left has neither a bridge-table nor a spatial match -- fill the rest of
    # this request's budget with plain extruded boxes from their 2D footprint instead of
    # leaving them absent, flagged "estimated" so the frontend can render them distinctly.
    remaining = MAX_BUILDINGS_3D_PER_REQUEST - len(by_building)
    if remaining > 0:
        estimated_rows = db.execute(
            text(
                """
                SELECT b.uuid AS bldg_uuid,
                       ST_AsGeoJSON(ST_Transform(b.geom, 4326), 7) AS footprint,
                       GREATEST(3, LEAST(100, COALESCE(
                           pb."Gross volume" / NULLIF(pb."Footprint area", 0),
                           pb."Storey number" * COALESCE(pb."Average Storey Height", 3),
                           9
                       ))) AS est_height
                FROM emc.building b
                LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
                WHERE b.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
                  AND NOT (b.uuid = ANY(:already_found))
                  AND NOT EXISTS (
                      SELECT 1 FROM emc.relation_bldg_citygml_v2 rel
                      JOIN citydb.thematic_surface ts ON ts.building_id = rel.citygml_bldg_id
                      WHERE rel.bldg_uuid = b.uuid AND ts.objectclass_id = ANY(:cls)
                  )
                LIMIT :limit
                """
            ),
            {
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
                "cls": list(SURFACE_KIND_3D),
                "already_found": list(by_building.keys()),
                "limit": remaining,
            },
        ).all()

        for r in estimated_rows:
            footprint = json.loads(r.footprint)
            surfaces = _extrude_footprint(footprint, float(r.est_height))
            by_building[r.bldg_uuid] = {"surfaces": surfaces, "estimated": True}
            pt = surfaces[0]["rings"][0][0]
            lon_sum += pt[0]
            lat_sum += pt[1]
            n += 1

    if not by_building:
        return {"origin": None, "buildings": []}

    return {
        "origin": [lon_sum / n, lat_sum / n],
        "buildings": [
            {"bldg_uuid": uuid, "surfaces": b["surfaces"], "estimated": b["estimated"]}
            for uuid, b in by_building.items()
        ],
    }


@app.get("/api/trees")
def list_trees(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat in WGS84"),
    db: Session = Depends(get_db),
):
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be 'min_lon,min_lat,max_lon,max_lat'")

    params = {
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "min_radius": MIN_CROWN_RADIUS_M,
        "limit": MAX_TREES_PER_REQUEST,
    }

    rows = db.execute(
        text(
            """
            SELECT * FROM (
                SELECT
                    gisid, art_dtsch, gattung_deutsch, kronedurch, baumhoehe, bezirk, 'strassenbaum' AS herkunft,
                    ST_AsGeoJSON(ST_Transform(
                        ST_Buffer(geom, GREATEST(kronedurch / 2, :min_radius)), 4326
                    )) AS geom
                FROM core_veg.strassenbaeume
                WHERE geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
                UNION ALL
                SELECT
                    gisid, art_dtsch, gattung_deutsch, kronedurch, baumhoehe, bezirk, 'anlagenbaum' AS herkunft,
                    ST_AsGeoJSON(ST_Transform(
                        ST_Buffer(geom, GREATEST(kronedurch / 2, :min_radius)), 4326
                    )) AS geom
                FROM core_veg.anlagenbaeume
                WHERE geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
            ) t
            LIMIT :limit
            """
        ),
        params,
    ).all()

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": json.loads(row.geom),
                "properties": {
                    "gisid": row.gisid,
                    "art": row.art_dtsch,
                    "gattung": row.gattung_deutsch,
                    "kronedurch": row.kronedurch,
                    "baumhoehe": row.baumhoehe,
                    "bezirk": row.bezirk,
                    "herkunft": row.herkunft,
                },
            }
            for row in rows
        ],
    }


@app.get("/api/chillers")
def list_chillers(
    bbox: str = Query(..., description="min_lon,min_lat,max_lon,max_lat in WGS84"),
    db: Session = Depends(get_db),
):
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(status_code=400, detail="bbox must be 'min_lon,min_lat,max_lon,max_lat'")

    rows = db.execute(
        text(
            """
            SELECT
                objectid, class AS class_name, confidence, shape_area,
                ST_AsGeoJSON(ST_Transform(geom, 4326)) AS geom
            FROM core_building."Chillers"
            WHERE geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
            LIMIT :limit
            """
        ),
        {
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
            "limit": MAX_CHILLERS_PER_REQUEST,
        },
    ).all()

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": json.loads(row.geom),
                "properties": {
                    "objectid": row.objectid,
                    "class": row.class_name,
                    "confidence": row.confidence,
                    "shape_area": row.shape_area,
                },
            }
            for row in rows
        ],
    }


class ChatRequest(BaseModel):
    message: str
    bbox: str
    polygon: dict | None = None


@app.post("/api/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    provider = os.getenv("CHAT_PROVIDER", "ollama").lower()
    if provider == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured on the server")
    return run_chat(db, req.message, req.bbox, req.polygon)
