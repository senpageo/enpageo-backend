"""Tools the chat agent can call. TOOLS is in Anthropic's tool schema format;
_to_openai_tools() below derives the OpenAI/Ollama function-calling format from the same
source so both providers share one definition."""

import json
from functools import lru_cache

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from .frames import field, frame


def _build_search_geom_expr(polygon: dict, buffer_distance_m: float | None, params: dict) -> str:
    """SQL expression (SRID 25833) for the drawn search area: a Polygon is used as-is; a
    Point or LineString is buffered by buffer_distance_m into an actual search area (a
    circle around a point, a corridor along a line)."""
    params["polygon_geojson"] = json.dumps(polygon)
    expr = "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:polygon_geojson), 4326), 25833)"
    if polygon.get("type") != "Polygon" and buffer_distance_m:
        params["buffer_distance_m"] = buffer_distance_m
        expr = f"ST_Buffer({expr}, :buffer_distance_m)"
    return expr


def _buffered_search_area_m2(db: Session, polygon: dict, buffer_distance_m: float | None) -> float | None:
    """Area of the buffered search geometry in m², only computed when a point/line was
    actually buffered (a hand-drawn polygon's area isn't reported back)."""
    if polygon.get("type") == "Polygon" or not buffer_distance_m:
        return None
    area_params: dict = {}
    expr = _build_search_geom_expr(polygon, buffer_distance_m, area_params)
    return round(db.execute(text(f"SELECT ST_Area({expr}) AS area"), area_params).one().area, 1)

_UMLAUT_FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def _fold_umlauts(s: str) -> str:
    """The building dataset's category strings are ASCII-transliterated German (e.g. 'Buero',
    'Gebaeude'). Fold real umlauts/ß in user/LLM input the same way so ILIKE matches regardless
    of which spelling was typed."""
    return s.translate(_UMLAUT_FOLD)


# Common paraphrases a user or LLM might use that are NOT literal substrings of the dataset's
# category strings (e.g. "Bürogebäude" -> "Buerogebaeude" isn't contained in "Buero-, Verwaltungs-
# oder Amtsgebaeude"). Maps a normalized (lowercased, umlaut-folded) keyword to the exact
# substring that does match, as a fallback when the given value isn't already a substring of a
# real category.
USAGE_ZONE_ALIASES = {
    "einfamilienhaus": "Einfamilien-/ Reihenhaus",
    "reihenhaus": "Einfamilien-/ Reihenhaus",
    "einfamilienhaeuser": "Einfamilien-/ Reihenhaus",
    "reihenhaeuser": "Einfamilien-/ Reihenhaus",
    "mehrfamilienhaus": "Mehrfamilienhaus",
    "mehrfamilienhaeuser": "Mehrfamilienhaus",
    "wohnhaus": "Mehrfamilienhaus",
    "apartmenthaus": "Mehrfamilienhaus",
    "buerogebaeude": "Buero",
    "buerohaus": "Buero",
    "verwaltungsgebaeude": "Buero",
    "amtsgebaeude": "Buero",
    "rathaus": "Buero",
    "handelsgebaeude": "Handelsgebaeude",
    "einkaufszentrum": "Handelsgebaeude",
    "laden": "Handelsgebaeude",
    "geschaeft": "Handelsgebaeude",
    "supermarkt": "Handelsgebaeude",
    "industriegebaeude": "Produktions",
    "fabrik": "Produktions",
    "lagergebaeude": "Produktions",
    "lagerhalle": "Produktions",
    "werkstatt": "Produktions",
    "betriebsgebaeude": "Produktions",
    "schule": "Schule",
    "kindergarten": "Betreuungsgebaeude",
    "kita": "Betreuungsgebaeude",
    "kindertagesstaette": "Betreuungsgebaeude",
    "hotel": "Beherberungs",
    "pension": "Beherberungs",
    "hostel": "Beherberungs",
    "sporthalle": "Sporthalle",
    "krankenhaus": "Krankenhaus",
    "klinik": "Krankenhaus",
    "pflegeheim": "Pflegeheim",
    "altenheim": "Pflegeheim",
    "restaurant": "Gastronomie",
    "gaststaette": "Gastronomie",
    "kantine": "Gastronomie",
}


def _resolve_usage_zone_type(value: str) -> str:
    """Fold umlauts, then fall back to USAGE_ZONE_ALIASES when the (folded) value isn't already a
    substring of a real category name, so common paraphrases don't silently return 0 matches."""
    folded = _fold_umlauts(value)
    key = folded.strip().lower().rstrip("e")  # drop a trailing plural/case 'e' before lookup
    return USAGE_ZONE_ALIASES.get(folded.strip().lower(), USAGE_ZONE_ALIASES.get(key, folded))


# Words that mean several categories at once (the expert questions have "Wohngebäude" and "sensible Gebäude").
USAGE_ZONE_GROUPS = {
    "wohngebaeude": ["Einfamilien-/ Reihenhaus", "Mehrfamilienhaus"],
    "wohnen": ["Einfamilien-/ Reihenhaus", "Mehrfamilienhaus"],
    "sensibel": ["Krankenhaus", "Pflegeheim", "Schule"],
    "sensible gebaeude": ["Krankenhaus", "Pflegeheim", "Schule"],
}


def usage_condition(value: str, params: dict, column: str = 'pb."PrimaryUsageZoneType"') -> str:
    """SQL condition for a usage filter; a group word (USAGE_ZONE_GROUPS) matches any of its categories."""
    key = _fold_umlauts(value).strip().lower()
    values = USAGE_ZONE_GROUPS.get(key) or [_resolve_usage_zone_type(value)]
    ors = []
    for k, v in enumerate(values):
        params[f"usage_{k}"] = f"%{v}%"
        ors.append(f"{column} ILIKE :usage_{k}")
    return "(" + " OR ".join(ors) + ")"


def district_condition(names: list[str], params: dict, geom: str = "b.geom") -> str:
    """Spatial filter on named Berlin districts (fast for a few districts, GiST on both sides)."""
    ors = []
    for k, name in enumerate(names):
        params[f"district_name_{k}"] = f"%{name}%"
        ors.append(f"d.name ILIKE :district_name_{k}")
    return f"EXISTS (SELECT 1 FROM emc.district d WHERE ({' OR '.join(ors)}) AND ST_Intersects(d.geom, {geom}))"


@lru_cache(maxsize=256)
def _resolve_place_cached(name: str) -> dict | None:
    try:
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": name, "format": "jsonv2", "limit": 1, "polygon_geojson": 1, "polygon_threshold": 0.0005,
                    "viewbox": "13.08,52.68,13.76,52.33", "bounded": 1},
            headers={"User-Agent": "enpageo-coolingmap/1.0"},
            timeout=10,
        )
        hits = r.json() if r.status_code == 200 else []
    except httpx.HTTPError:
        return None
    if not hits:
        return None
    return {"geometry": hits[0]["geojson"], "label": hits[0].get("display_name", name)}


def resolve_place(name: str) -> dict | None:
    """Named place in Berlin -> GeoJSON geometry (OpenStreetMap Nominatim; only the place name is sent)."""
    return _resolve_place_cached(name.strip().lower())


MAX_RESULTS = 500
DEFAULT_RESULTS = 200
MIN_CROWN_RADIUS_M = 0.5

# July, matching the single fixed month calib_monthly used to report ("cooling_demand_07")
# before it was dropped — see core_bldg.usage_cooling_profile and _execute_find_buildings.
COOLING_DEMAND_EXPR = (
    'COALESCE(pb."PrimaryUsageZoneArea", pb."Heated area", 0) '
    "* COALESCE(ucp.specific_annual_cooling_kwh_m2, 20) "
    "* COALESCE(ucp.pct_07, 30) / 100"
)
COOLING_ANNUAL_EXPR = 'COALESCE(pb."PrimaryUsageZoneArea", pb."Heated area", 0) * COALESCE(ucp.specific_annual_cooling_kwh_m2, 20)'
# Required cooling capacity in kW: generic specific capacity per usage type (W/m²) × area. Placeholder like the demand.
COOLING_CAPACITY_EXPR = 'COALESCE(pb."PrimaryUsageZoneArea", pb."Heated area", 0) * COALESCE(ucp.specific_cooling_capacity_w_m2, 0) / 1000'
HAS_CHILLER_EXPR = 'EXISTS (SELECT 1 FROM core_building."Chillers" c WHERE ST_Intersects(c.geom, b.geom))'
FIND_BUILDINGS_SORT = {
    "cooling_annual": "cooling_annual",
    "cooling_capacity": "cooling_capacity_kw",
    "heating_demand": "heating_demand",
    "year_built": "year_built_int",
}

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
                "'Unbeheizt' (unheated). Group words also work: 'Wohngebaeude' (both house types), "
                "'sensibel' (hospitals, care homes, schools)."
            ),
        },
        "min_cooling_capacity_kw": {"type": "number", "description": "Minimum required cooling capacity in kW (1 MW = 1000), generic placeholder"},
        "max_cooling_capacity_kw": {"type": "number", "description": "Maximum required cooling capacity in kW"},
        "has_chiller": {"type": "boolean", "description": "true: only buildings with a rooftop chiller detected from aerial imagery; false: only buildings without one"},
        "districts": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these Berlin districts (partial names); overrides the map view"},
        "scope": {"type": "string", "enum": ["view", "berlin"], "description": "'view' (default): drawn area or visible map. 'berlin': all of Berlin"},
        "near_place": {"type": "string", "description": "Named place in Berlin (e.g. 'Tiergarten'); use with max_distance_m"},
        "max_distance_m": {"type": "number", "description": "With near_place: maximum distance in meters (3 km = 3000)"},
        "sort_by": {"type": "string", "enum": list(FIND_BUILDINGS_SORT), "description": "What the largest results are sorted by (rankings, 'höchster ...'); default cooling_annual"},
        "min_cooling_demand": {"type": "number", "description": "Minimum July cooling demand in kWh"},
        "max_cooling_demand": {"type": "number", "description": "Maximum July cooling demand in kWh"},
        "min_heating_demand": {"type": "number", "description": "Minimum yearly heating demand in kWh"},
        "max_heating_demand": {"type": "number", "description": "Maximum yearly heating demand in kWh"},
        "min_year_built": {"type": "integer", "description": "Earliest year of construction"},
        "max_year_built": {"type": "integer", "description": "Latest year of construction"},
        "buffer_distance_m": {
            "type": "number",
            "description": (
                "ONLY when the user drew a point or line (not a polygon) and named a distance in "
                "their message, e.g. 'im Abstand von 100m', 'im Radius von 10m', 'within 50m': the "
                "distance in meters to buffer the drawn point/line into a search area (a circle "
                "around a point, a corridor along a line). Omit entirely for a drawn polygon or the "
                "map viewport, or if no distance was mentioned."
            ),
        },
        "limit": {
            "type": "integer",
            "description": f"Max buildings to return, default {DEFAULT_RESULTS}, max {MAX_RESULTS}",
        },
    },
    "required": [],
}

FIND_TREES_SCHEMA = {
    "type": "object",
    "properties": {
        "art": {
            "type": "string",
            "description": (
                "Partial match (ILIKE) against the German tree species name (art_dtsch), e.g. "
                "'Eiche', 'Linde', 'Ahorn', 'Platane'. Use a genus-level substring, not a full "
                "cultivar name, unless the user names one exactly."
            ),
        },
        "herkunft": {
            "type": "string",
            "enum": ["strassenbaum", "anlagenbaum"],
            "description": (
                "Restrict to street trees ('strassenbaum') or park/facility trees ('anlagenbaum'). "
                "Omit to search both."
            ),
        },
        "bezirk": {"type": "string", "description": "Partial match against the Berlin district name"},
        "min_kronedurch": {"type": "number", "description": "Minimum crown diameter in meters"},
        "max_kronedurch": {"type": "number", "description": "Maximum crown diameter in meters"},
        "min_baumhoehe": {"type": "number", "description": "Minimum tree height in meters"},
        "max_baumhoehe": {"type": "number", "description": "Maximum tree height in meters"},
        "buffer_distance_m": {
            "type": "number",
            "description": (
                "ONLY when the user drew a point or line (not a polygon) and named a distance in "
                "their message, e.g. 'im Abstand von 100m', 'im Radius von 10m', 'within 50m': the "
                "distance in meters to buffer the drawn point/line into a search area (a circle "
                "around a point, a corridor along a line). Omit entirely for a drawn polygon or the "
                "map viewport, or if no distance was mentioned."
            ),
        },
        "limit": {
            "type": "integer",
            "description": f"Max trees to return, default {DEFAULT_RESULTS}, max {MAX_RESULTS}",
        },
    },
    "required": [],
}

CHILLER_CLASSES = [
    "ACC_Large",
    "CT_Base_induced",
    "CT_Fan_induced",
    "Hybrid_supposed",
    "CT_forced",
    "CT_forced_difficult",
]

FIND_CHILLERS_SCHEMA = {
    "type": "object",
    "properties": {
        "chiller_class": {
            "type": "string",
            "enum": CHILLER_CLASSES,
            "description": (
                "Exact class from the aerial-imagery detection: 'ACC_Large' (large air-cooled "
                "condenser), 'CT_Base_induced'/'CT_Fan_induced'/'CT_forced'/'CT_forced_difficult' "
                "(cooling tower variants), 'Hybrid_supposed' (suspected hybrid unit). Omit to search "
                "all classes."
            ),
        },
        "min_confidence": {
            "type": "number",
            "description": "Minimum detection confidence, 0-100",
        },
        "districts": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these Berlin districts (partial names)"},
        "scope": {"type": "string", "enum": ["view", "berlin"], "description": "'berlin': all of Berlin instead of the visible map"},
        "min_shape_area": {"type": "number", "description": "Minimum footprint area in square meters"},
        "max_shape_area": {"type": "number", "description": "Maximum footprint area in square meters"},
        "buffer_distance_m": {
            "type": "number",
            "description": (
                "ONLY when the user drew a point or line (not a polygon) and named a distance in "
                "their message, e.g. 'im Abstand von 100m', 'im Radius von 10m', 'within 50m': the "
                "distance in meters to buffer the drawn point/line into a search area (a circle "
                "around a point, a corridor along a line). Omit entirely for a drawn polygon or the "
                "map viewport, or if no distance was mentioned."
            ),
        },
        "limit": {
            "type": "integer",
            "description": f"Max chillers to return, default {DEFAULT_RESULTS}, max {MAX_RESULTS}",
        },
    },
    "required": [],
}

TOOLS = [
    {
        "name": "find_buildings",
        "description": (
            "Find buildings in the search area (a user-drawn polygon/point/line if one exists, "
            "otherwise the currently visible map area) matching filters, and show them on the map. "
            "Heating demand is a real building-specific annual estimate; cooling demand is a "
            "generic per-usage-type placeholder (not a simulation result). If a point or line was drawn "
            "and the user gave a distance ('im Abstand von 100m', 'im Radius von 10m'), pass it as "
            "buffer_distance_m to search within that distance. Use this whenever the user asks about "
            "specific buildings by usage type, heating/cooling demand, or year built."
        ),
        "input_schema": FIND_BUILDINGS_SCHEMA,
    },
    {
        "name": "find_trees",
        "description": (
            "Find street or park trees (Berlin Baumbestand data) in the search area (a user-drawn "
            "polygon/point/line if one exists, otherwise the currently visible map area) matching "
            "filters, and show them on the map as their real crown-diameter circles. If a point or "
            "line was drawn and the user gave a distance ('im Abstand von 100m', 'im Radius von "
            "10m'), pass it as buffer_distance_m to search within that distance. Use this whenever "
            "the user asks about trees by species, crown diameter, height, district, or street vs. "
            "park trees."
        ),
        "input_schema": FIND_TREES_SCHEMA,
    },
    {
        "name": "find_chillers",
        "description": (
            "Find rooftop cooling equipment (air-cooled condensers / cooling towers, detected from "
            "aerial imagery, Berlin-wide) in the search area (a user-drawn polygon/point/line if one "
            "exists, otherwise the currently visible map area) matching filters, and show them on the "
            "map. If a point or line was drawn and the user gave a distance ('im Abstand von 100m', "
            "'im Radius von 10m'), pass it as buffer_distance_m to search within that distance. Use "
            "this whenever the user asks about chillers, cooling towers, condensers, or rooftop "
            "cooling infrastructure."
        ),
        "input_schema": FIND_CHILLERS_SCHEMA,
    },
]


def to_openai_tools(tools: list[dict] | None = None) -> list[dict]:
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
        for t in (tools if tools is not None else TOOLS)
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
    buffer_distance_m: float | None = None,
    limit: int | None = None,
    min_cooling_capacity_kw: float | None = None,
    max_cooling_capacity_kw: float | None = None,
    has_chiller: bool | None = None,
    districts: list[str] | None = None,
    scope: str | None = None,
    near_place: str | None = None,
    max_distance_m: float | None = None,
    sort_by: str | None = None,
) -> dict:
    limit = min(int(limit or DEFAULT_RESULTS), MAX_RESULTS)

    conditions = []
    params: dict = {"limit": limit}
    notes: list[str] = []
    sources = ["footprints", "building_params", "energymap_heating", "cooling_placeholder"]

    if near_place:
        place = resolve_place(near_place)
        if not place:
            return {"error": f"Ort '{near_place}' wurde in Berlin nicht gefunden"}
        params["place_geojson"] = json.dumps(place["geometry"])
        params["max_distance_m"] = float(max_distance_m or 1000)
        conditions.append(
            "ST_DWithin(b.geom, ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:place_geojson), 4326), 25833), :max_distance_m)"
        )
        notes.append(f"Ort aufgelöst als: {place['label']}")
        sources.append("places")
    elif districts:
        conditions.append(district_condition(districts, params))
        sources.append("districts")
    elif scope == "berlin":
        pass
    elif polygon:
        # A user-drawn polygon/point/line is more precise than the map viewport — use it instead of bbox.
        search_geom = _build_search_geom_expr(polygon, buffer_distance_m, params)
        conditions.append(f"ST_Intersects(b.geom, {search_geom})")
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
        conditions.append(usage_condition(usage_zone_type, params))
    if min_cooling_capacity_kw is not None:
        conditions.append(f"({COOLING_CAPACITY_EXPR}) >= :min_cooling_capacity_kw")
        params["min_cooling_capacity_kw"] = min_cooling_capacity_kw
    if max_cooling_capacity_kw is not None:
        conditions.append(f"({COOLING_CAPACITY_EXPR}) <= :max_cooling_capacity_kw")
        params["max_cooling_capacity_kw"] = max_cooling_capacity_kw
    if has_chiller is not None:
        conditions.append(HAS_CHILLER_EXPR if has_chiller else f"NOT {HAS_CHILLER_EXPR}")
        sources.append("chillers")
        if has_chiller is False:
            notes.append("„Keine Kühlanlage“ heißt: im Luftbild nichts erkannt. Innenliegende Anlagen sieht die Erkennung nicht.")
    if min_cooling_demand is not None:
        conditions.append(f"({COOLING_DEMAND_EXPR}) >= :min_cooling_demand")
        params["min_cooling_demand"] = min_cooling_demand
    if max_cooling_demand is not None:
        conditions.append(f"({COOLING_DEMAND_EXPR}) <= :max_cooling_demand")
        params["max_cooling_demand"] = max_cooling_demand
    if min_heating_demand is not None:
        conditions.append("COALESCE(el.cons_c2r2, 0) >= :min_heating_demand")
        params["min_heating_demand"] = min_heating_demand
    if max_heating_demand is not None:
        conditions.append("COALESCE(el.cons_c2r2, 0) <= :max_heating_demand")
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

    # No per-building simulation result remains for cooling (core_bldg.calib_monthly was
    # dropped — 36 GB, 221 of 222 climate/refurbishment combinations never queried by this
    # tool, and its numbers were themselves estimates, not measurements). Heating stays
    # building-specific via emp.emblive (real per-building annual demand); cooling now comes
    # from core_bldg.usage_cooling_profile, a small per-usage-type placeholder — see that
    # table's comment for the reasoning and cooling_demand_07 for why "*_07" (July).
    base_from = """
        FROM emc.building b
        LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
        LEFT JOIN emp.emblive el ON el.uuid = b.uuid
        LEFT JOIN core_bldg.usage_cooling_profile ucp ON ucp.usage_zone_type = pb."PrimaryUsageZoneType"
        WHERE 1=1
    """

    total = db.execute(text(f"SELECT count(*) AS total {base_from} {where_extra}"), params).one().total

    rows = db.execute(
        text(
            f"""
            SELECT
                b.uuid AS bldg_uuid,
                ST_AsGeoJSON(ST_Transform(b.geom, 4326)) AS geom,
                ({COOLING_DEMAND_EXPR}) AS cooling_demand,
                ({COOLING_ANNUAL_EXPR}) AS cooling_annual,
                ({COOLING_CAPACITY_EXPR}) AS cooling_capacity_kw,
                el.cons_c2r2 AS heating_demand,
                pb."PrimaryUsageZoneType" AS usage_zone_type,
                pb."Year of construction" AS year_built,
                CASE WHEN pb."Year of construction" ~ '^[0-9]+$' THEN pb."Year of construction"::int END AS year_built_int
            {base_from} {where_extra}
            ORDER BY {FIND_BUILDINGS_SORT.get(sort_by or "", "cooling_annual")} DESC NULLS LAST
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
                "cooling_annual": row.cooling_annual,
                "cooling_capacity_kw": row.cooling_capacity_kw,
                "heating_demand": row.heating_demand,
                "usage_zone_type": row.usage_zone_type,
                "year_built": row.year_built,
            },
        }
        for row in rows
    ]

    cooling_values = [f["properties"]["cooling_demand"] for f in features if f["properties"]["cooling_demand"] is not None]
    heating_values = [f["properties"]["heating_demand"] for f in features if f["properties"]["heating_demand"] is not None]

    # Typed result for the presentation chooser (frames.py); geometry stays out of it, the map joins by uuid.
    building_frame = frame(
        "features",
        [
            field("bldg_uuid", "id", "Gebäude-ID"),
            field("usage_zone_type", "nominal", "Nutzungsart"),
            field("year_built", "ordinal", "Baujahr"),
            field("heating_demand", "quantitative", "Heizwärmebedarf", "kWh/a"),
            field("cooling_annual", "quantitative", "Kältebedarf (Platzhalter)", "kWh/a"),
            field("cooling_capacity_kw", "quantitative", "Kälteleistung (Platzhalter)", "kW"),
        ],
        [
            {k: (round(float(v), 1) if k in ("heating_demand", "cooling_annual", "cooling_capacity_kw") and v is not None else v)
             for k, v in f["properties"].items() if k != "cooling_demand"}
            for f in features
        ],
        sources,
        ids=[f["properties"]["bldg_uuid"] for f in features],
        total=total,
        query={"usage_zone_type": usage_zone_type, "min_cooling_demand": min_cooling_demand,
               "max_cooling_demand": max_cooling_demand, "min_heating_demand": min_heating_demand,
               "max_heating_demand": max_heating_demand, "min_year_built": min_year_built,
               "max_year_built": max_year_built, "buffer_distance_m": buffer_distance_m,
               "min_cooling_capacity_kw": min_cooling_capacity_kw, "max_cooling_capacity_kw": max_cooling_capacity_kw,
               "has_chiller": has_chiller, "districts": districts, "scope": scope, "near_place": near_place,
               "max_distance_m": max_distance_m, "sort_by": sort_by},
        notes=notes + ["Kältebedarf und Kälteleistung sind generische Platzhalter je Nutzungsart, keine Simulation."],
    )

    return {
        "layer": "buildings",
        "geojson": {"type": "FeatureCollection", "features": features},
        "frame": building_frame,
        "summary_for_llm": {
            "total_matches": total,
            "returned": len(features),
            "truncated": total > len(features),
            "avg_cooling_demand_kwh": round(sum(cooling_values) / len(cooling_values), 1) if cooling_values else None,
            "avg_heating_demand_kwh": round(sum(heating_values) / len(heating_values), 1) if heating_values else None,
            "search_area_m2": _buffered_search_area_m2(db, polygon, buffer_distance_m) if polygon else None,
        },
    }


def _execute_find_trees(
    db: Session,
    bbox: str | None,
    polygon: dict | None = None,
    art: str | None = None,
    herkunft: str | None = None,
    bezirk: str | None = None,
    min_kronedurch: float | None = None,
    max_kronedurch: float | None = None,
    min_baumhoehe: float | None = None,
    max_baumhoehe: float | None = None,
    buffer_distance_m: float | None = None,
    limit: int | None = None,
) -> dict:
    limit = min(int(limit or DEFAULT_RESULTS), MAX_RESULTS)

    conditions = []
    params: dict = {"min_radius": MIN_CROWN_RADIUS_M, "limit": limit}

    if polygon:
        search_geom = _build_search_geom_expr(polygon, buffer_distance_m, params)
        conditions.append(f"ST_Intersects(geom, {search_geom})")
    elif bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
            conditions.append(
                "geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)"
            )
            params.update({"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat})
        except ValueError:
            pass

    if art:
        conditions.append("art_dtsch ILIKE :art")
        params["art"] = f"%{art}%"
    if bezirk:
        conditions.append("bezirk ILIKE :bezirk")
        params["bezirk"] = f"%{bezirk}%"
    if min_kronedurch is not None:
        conditions.append("kronedurch >= :min_kronedurch")
        params["min_kronedurch"] = min_kronedurch
    if max_kronedurch is not None:
        conditions.append("kronedurch <= :max_kronedurch")
        params["max_kronedurch"] = max_kronedurch
    if min_baumhoehe is not None:
        conditions.append("baumhoehe >= :min_baumhoehe")
        params["min_baumhoehe"] = min_baumhoehe
    if max_baumhoehe is not None:
        conditions.append("baumhoehe <= :max_baumhoehe")
        params["max_baumhoehe"] = max_baumhoehe

    where_extra = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    tables = []
    if herkunft in (None, "strassenbaum"):
        tables.append(("core_veg.strassenbaeume", "strassenbaum"))
    if herkunft in (None, "anlagenbaum"):
        tables.append(("core_veg.anlagenbaeume", "anlagenbaum"))

    union_sql = " UNION ALL ".join(
        f"""
        SELECT gisid, art_dtsch, gattung_deutsch, kronedurch, baumhoehe, bezirk, '{herkunft_val}' AS herkunft,
               ST_Buffer(geom, GREATEST(kronedurch / 2, :min_radius)) AS crown_geom
        FROM {table} {where_extra}
        """
        for table, herkunft_val in tables
    )

    total = db.execute(text(f"SELECT count(*) AS total FROM ({union_sql}) t"), params).one().total

    rows = db.execute(
        text(
            f"""
            SELECT gisid, art_dtsch, gattung_deutsch, kronedurch, baumhoehe, bezirk, herkunft,
                   ST_AsGeoJSON(ST_Transform(crown_geom, 4326)) AS geom
            FROM ({union_sql}) t
            ORDER BY kronedurch DESC NULLS LAST
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
    ]

    kronedurch_values = [f["properties"]["kronedurch"] for f in features if f["properties"]["kronedurch"] is not None]

    return {
        "layer": "trees",
        "geojson": {"type": "FeatureCollection", "features": features},
        "summary_for_llm": {
            "total_matches": total,
            "returned": len(features),
            "truncated": total > len(features),
            "avg_kronedurch_m": round(sum(kronedurch_values) / len(kronedurch_values), 1) if kronedurch_values else None,
            "search_area_m2": _buffered_search_area_m2(db, polygon, buffer_distance_m) if polygon else None,
        },
    }


def _execute_find_chillers(
    db: Session,
    bbox: str | None,
    polygon: dict | None = None,
    chiller_class: str | None = None,
    min_confidence: float | None = None,
    min_shape_area: float | None = None,
    max_shape_area: float | None = None,
    buffer_distance_m: float | None = None,
    limit: int | None = None,
    districts: list[str] | None = None,
    scope: str | None = None,
) -> dict:
    limit = min(int(limit or DEFAULT_RESULTS), MAX_RESULTS)

    conditions = []
    params: dict = {"limit": limit}

    if districts:
        conditions.append(district_condition(districts, params, "geom"))
    elif scope == "berlin":
        pass
    elif polygon:
        search_geom = _build_search_geom_expr(polygon, buffer_distance_m, params)
        conditions.append(f"ST_Intersects(geom, {search_geom})")
    elif bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
            conditions.append(
                "geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)"
            )
            params.update({"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat})
        except ValueError:
            pass

    if chiller_class:
        conditions.append("class = :chiller_class")
        params["chiller_class"] = chiller_class
    if min_confidence is not None:
        conditions.append("confidence >= :min_confidence")
        params["min_confidence"] = min_confidence
    if min_shape_area is not None:
        conditions.append("shape_area >= :min_shape_area")
        params["min_shape_area"] = min_shape_area
    if max_shape_area is not None:
        conditions.append("shape_area <= :max_shape_area")
        params["max_shape_area"] = max_shape_area

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = db.execute(
        text(f'SELECT count(*) AS total FROM core_building."Chillers" {where_clause}'), params
    ).one().total

    rows = db.execute(
        text(
            f"""
            SELECT objectid, class AS class_name, confidence, shape_area,
                   ST_AsGeoJSON(ST_Transform(geom, 4326)) AS geom
            FROM core_building."Chillers"
            {where_clause}
            ORDER BY shape_area DESC NULLS LAST
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
                "objectid": row.objectid,
                "class": row.class_name,
                "confidence": row.confidence,
                "shape_area": row.shape_area,
            },
        }
        for row in rows
    ]

    chiller_frame = frame(
        "features",
        [
            field("objectid", "id", "Objekt-ID"),
            field("class", "nominal", "Klasse"),
            field("confidence", "quantitative", "Erkennungssicherheit", "%"),
            field("shape_area", "quantitative", "Grundfläche", "m²"),
        ],
        [{k: (round(float(v), 1) if k in ("confidence", "shape_area") and v is not None else v)
          for k, v in f["properties"].items()} for f in features],
        ["chillers"] + (["districts"] if districts else []),
        ids=[str(f["properties"]["objectid"]) for f in features],
        total=total,
        query={"chiller_class": chiller_class, "districts": districts, "scope": scope},
        notes=["Die Kälteleistung je Anlage liegt noch nicht vor (kommt vom KIT)."],
    )

    return {
        "layer": "chillers",
        "geojson": {"type": "FeatureCollection", "features": features},
        "frame": chiller_frame,
        "summary_for_llm": {
            "total_matches": total,
            "returned": len(features),
            "truncated": total > len(features),
            "search_area_m2": _buffered_search_area_m2(db, polygon, buffer_distance_m) if polygon else None,
        },
    }


def execute_tool(name: str, tool_input: dict, db: Session, bbox: str | None, polygon: dict | None = None) -> dict:
    if name == "find_buildings":
        return _execute_find_buildings(db, bbox, polygon, **tool_input)
    if name == "find_trees":
        return _execute_find_trees(db, bbox, polygon, **tool_input)
    if name == "find_chillers":
        return _execute_find_chillers(db, bbox, polygon, **tool_input)
    from . import analysis  # imported here: analysis reuses helpers from this module

    result = analysis.execute(name, tool_input, db, bbox, polygon)
    return result if result is not None else {"error": f"unknown tool {name}"}
