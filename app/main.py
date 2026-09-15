import json
import os

from dotenv import load_dotenv
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

# Default scenario slice for the building demand map: chosen with the user
# (2040 / RCP 4.5 / Medium refurbishment) — one row per building in core_bldg.calib_monthly.
DEFAULT_CLIMATE_YEAR = 2040
DEFAULT_CLIMATE_SCENARIO = "RCP_4_5"
DEFAULT_REFURBISHMENT_VARIANT = "Medium"
MAX_BUILDINGS_PER_REQUEST = 5000
MAX_TREES_PER_REQUEST = 8000
MIN_CROWN_RADIUS_M = 0.5
MAX_CHILLERS_PER_REQUEST = 5437

load_dotenv()

app = FastAPI(title="Enpageo Map API")

cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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

    month_col = f"cooling_demand_{month:02d}"

    rows = db.execute(
        text(
            f"""
            SELECT
                b.uuid AS bldg_uuid,
                ST_AsGeoJSON(ST_Transform(b.geom, 4326)) AS geom,
                cm."{month_col}" AS cooling_demand,
                pb."PrimaryUsageZoneType" AS usage_zone_type
            FROM emc.building b
            JOIN core_bldg.calib_monthly cm ON cm.bldg_uuid = b.uuid
            LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
            WHERE cm."Climate Year" = :climate_year
              AND cm."Climate Scenario" = :climate_scenario
              AND cm."Refurbishment Variant" = :refurbishment_variant
              AND b.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)
            LIMIT :limit
            """
        ),
        {
            "climate_year": DEFAULT_CLIMATE_YEAR,
            "climate_scenario": DEFAULT_CLIMATE_SCENARIO,
            "refurbishment_variant": DEFAULT_REFURBISHMENT_VARIANT,
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
