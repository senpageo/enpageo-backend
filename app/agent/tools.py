"""Tools the chat agent can call. TOOLS is in Anthropic's tool schema format;
_to_openai_tools() below derives the OpenAI/Ollama function-calling format from the same
source so both providers share one definition."""

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_CLIMATE_YEAR = 2040
DEFAULT_CLIMATE_SCENARIO = "RCP_4_5"
DEFAULT_REFURBISHMENT_VARIANT = "Medium"
MAX_RESULTS = 500
DEFAULT_RESULTS = 200

FIND_BUILDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "usage_zone_type": {
            "type": "string",
            "description": (
                "Partial match (ILIKE) against the exact German category strings used in the dataset — "
                "use a substring from ONE of these real values, never a generic paraphrase like "
                "'Wohngebaeude' or 'Buero' alone (they won't match): "
                "'Einfamilien-/ Reihenhaus' (single/row houses), "
                "'(Grosses) Mehrfamilienhaus' (large apartment building), "
                "'Produktions-, Werkstatt-, Lager- oder Betriebsgebaeude' (industrial/warehouse), "
                "'Handelsgebaeude' (retail), "
                "'Buero-, Verwaltungs- oder Amtsgebaeude' (office/admin), "
                "'Schule, Kindertagesstaette und sonstige Betreuungsgebaeude' (school/daycare), "
                "'Beherberungs- oder Unterbringungsgebaeude' (lodging), "
                "'Sporthalle', 'Krankenhaus', 'Pflegeheim', 'Gastronomie- oder Verpflegungsgebaeude', "
                "'Unbeheizt' (unheated). For 'residential' pick BOTH house types (call twice or omit "
                "this filter and rely on the numeric filters instead)."
            ),
        },
        "min_cooling_demand": {"type": "number", "description": "Minimum July cooling demand in kWh"},
        "max_cooling_demand": {"type": "number", "description": "Maximum July cooling demand in kWh"},
        "min_heating_demand": {"type": "number", "description": "Minimum yearly heating demand in kWh"},
        "max_heating_demand": {"type": "number", "description": "Maximum yearly heating demand in kWh"},
        "min_year_built": {"type": "integer", "description": "Earliest year of construction"},
        "max_year_built": {"type": "integer", "description": "Latest year of construction"},
        "limit": {
            "type": "integer",
            "description": f"Max buildings to return, default {DEFAULT_RESULTS}, max {MAX_RESULTS}",
        },
    },
    "required": [],
}

TOOLS = [
    {
        "name": "find_buildings",
        "description": (
            "Find buildings in the search area (a user-drawn polygon if one exists, otherwise the "
            "currently visible map area; scenario: year 2040, climate RCP_4_5, medium refurbishment) "
            "matching filters, and show them on the map. Use this whenever the user asks about "
            "specific buildings by usage type, heating/cooling demand, or year built."
        ),
        "input_schema": FIND_BUILDINGS_SCHEMA,
    }
]


def to_openai_tools() -> list[dict]:
    """Same tool definitions, reshaped for the OpenAI/Ollama function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


def _execute_find_buildings(
    db: Session,
    bbox: str | None,
    polygon: dict | None = None,
    usage_zone_type: str | None = None,
    min_cooling_demand: float | None = None,
    max_cooling_demand: float | None = None,
    min_heating_demand: float | None = None,
    max_heating_demand: float | None = None,
    min_year_built: int | None = None,
    max_year_built: int | None = None,
    limit: int | None = None,
) -> dict:
    limit = min(int(limit or DEFAULT_RESULTS), MAX_RESULTS)

    conditions = []
    params: dict = {
        "climate_year": DEFAULT_CLIMATE_YEAR,
        "climate_scenario": DEFAULT_CLIMATE_SCENARIO,
        "refurbishment_variant": DEFAULT_REFURBISHMENT_VARIANT,
        "limit": limit,
    }

    if polygon:
        # A user-drawn polygon is more precise than the map viewport — use it instead of bbox.
        conditions.append(
            "ST_Intersects(b.geom, ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:polygon_geojson), 4326), 25833))"
        )
        params["polygon_geojson"] = json.dumps(polygon)
    elif bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
            conditions.append(
                "b.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)"
            )
            params.update({"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat})
        except ValueError:
            pass

    if usage_zone_type:
        conditions.append('pb."PrimaryUsageZoneType" ILIKE :usage_zone_type')
        params["usage_zone_type"] = f"%{usage_zone_type}%"
    if min_cooling_demand is not None:
        conditions.append("cm.cooling_demand_07 >= :min_cooling_demand")
        params["min_cooling_demand"] = min_cooling_demand
    if max_cooling_demand is not None:
        conditions.append("cm.cooling_demand_07 <= :max_cooling_demand")
        params["max_cooling_demand"] = max_cooling_demand
    if min_heating_demand is not None:
        conditions.append(
            'COALESCE(cm."Calibrated Yearly Heating demand", cm."Yearly Heating demand", 0) >= :min_heating_demand'
        )
        params["min_heating_demand"] = min_heating_demand
    if max_heating_demand is not None:
        conditions.append(
            'COALESCE(cm."Calibrated Yearly Heating demand", cm."Yearly Heating demand", 0) <= :max_heating_demand'
        )
        params["max_heating_demand"] = max_heating_demand
    if min_year_built is not None:
        conditions.append(
            '(pb."Year of construction" ~ \'^[0-9]+$\' AND pb."Year of construction"::int >= :min_year_built)'
        )
        params["min_year_built"] = min_year_built
    if max_year_built is not None:
        conditions.append(
            '(pb."Year of construction" ~ \'^[0-9]+$\' AND pb."Year of construction"::int <= :max_year_built)'
        )
        params["max_year_built"] = max_year_built

    where_extra = f"AND {' AND '.join(conditions)}" if conditions else ""

    base_from = """
        FROM emc.building b
        JOIN core_bldg.calib_monthly cm ON cm.bldg_uuid = b.uuid
        LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
        WHERE cm."Climate Year" = :climate_year
          AND cm."Climate Scenario" = :climate_scenario
          AND cm."Refurbishment Variant" = :refurbishment_variant
    """

    total = db.execute(text(f"SELECT count(*) AS total {base_from} {where_extra}"), params).one().total

    rows = db.execute(
        text(
            f"""
            SELECT
                b.uuid AS bldg_uuid,
                ST_AsGeoJSON(ST_Transform(b.geom, 4326)) AS geom,
                cm.cooling_demand_07 AS cooling_demand,
                COALESCE(cm."Calibrated Yearly Heating demand", cm."Yearly Heating demand") AS heating_demand,
                pb."PrimaryUsageZoneType" AS usage_zone_type,
                pb."Year of construction" AS year_built
            {base_from} {where_extra}
            ORDER BY cm.cooling_demand_07 DESC NULLS LAST
            LIMIT :limit
            """
        ),
        params,
    ).all()

    features = [
        {
            "type": "Feature",
            "geometry": json.loads(row.geom),
            "properties": {
                "bldg_uuid": row.bldg_uuid,
                "cooling_demand": row.cooling_demand,
                "heating_demand": row.heating_demand,
                "usage_zone_type": row.usage_zone_type,
                "year_built": row.year_built,
            },
        }
        for row in rows
    ]

    cooling_values = [f["properties"]["cooling_demand"] for f in features if f["properties"]["cooling_demand"] is not None]
    heating_values = [f["properties"]["heating_demand"] for f in features if f["properties"]["heating_demand"] is not None]

    return {
        "geojson": {"type": "FeatureCollection", "features": features},
        "summary_for_llm": {
            "total_matches": total,
            "returned": len(features),
            "truncated": total > len(features),
            "avg_cooling_demand_kwh": round(sum(cooling_values) / len(cooling_values), 1) if cooling_values else None,
            "avg_heating_demand_kwh": round(sum(heating_values) / len(heating_values), 1) if heating_values else None,
        },
    }


def execute_tool(name: str, tool_input: dict, db: Session, bbox: str | None, polygon: dict | None = None) -> dict:
    if name == "find_buildings":
        return _execute_find_buildings(db, bbox, polygon, **tool_input)
    return {"error": f"unknown tool {name}"}
