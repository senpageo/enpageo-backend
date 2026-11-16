"""Aggregating tools for the chat agent: questions that are answered with groups or a time series rather than
with individual buildings ("heating demand by usage type", "cooling demand over the year", "compare Neukölln
and Mitte"). Read-only and parameterized like the find_* tools in tools.py -- no free SQL.

Both return result frames (frames.py), so the presentation chooser can turn them into a bar, line or sunburst
chart, a table or a sentence.
"""
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from .frames import field, frame
from .tools import COOLING_ANNUAL_EXPR, COOLING_CAPACITY_EXPR, _build_search_geom_expr, usage_condition

MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

COOLING_AREA_EXPR = 'COALESCE(pb."PrimaryUsageZoneArea", pb."Heated area", 0)'

# Construction periods, roughly the German building-typology age classes.
PERIODS = [
    ("bis 1918", None, 1918),
    ("1919–1948", 1919, 1948),
    ("1949–1978", 1949, 1978),
    ("1979–1994", 1979, 1994),
    ("1995–2009", 1995, 2009),
    ("ab 2010", 2010, None),
]
_YEAR = 'CASE WHEN pb."Year of construction" ~ \'^[0-9]+$\' THEN pb."Year of construction"::int END'
PERIOD_EXPR = (
    "CASE "
    + " ".join(
        f"WHEN {_YEAR} {'<= ' + str(hi) if lo is None else ('>= ' + str(lo) if hi is None else f'BETWEEN {lo} AND {hi}')} "
        f"THEN '{label}'"
        for label, lo, hi in PERIODS
    )
    + " ELSE 'unbekannt' END"
)

GROUPS = {
    "usage_type": ('pb."PrimaryUsageZoneType"', field("usage_type", "nominal", "Nutzungsart")),
    "district": ("d.name", field("district", "nominal", "Bezirk")),
    "construction_period": (PERIOD_EXPR, field("construction_period", "ordinal", "Baualtersklasse")),
    "postcode": ("e.postcode::text", field("postcode", "nominal", "Postleitzahl")),
}

# metric -> (SQL aggregate, field, source ids). heating_specific is area-weighted (sum demand / sum area),
# because a plain mean of per-building ratios over-weights small buildings.
METRICS = {
    "heating_demand": ("SUM(e.cons_c2r2)", field("heating_demand", "quantitative", "Heizwärmebedarf", "kWh/a"),
                       ["energymap_heating"]),
    "heating_specific": ("SUM(e.cons_c2r2) / NULLIF(SUM(CASE WHEN e.cons_c2r2 IS NOT NULL THEN e.nrf_area END), 0)",
                         field("heating_specific", "quantitative", "spez. Heizwärmebedarf", "kWh/(m²·a)"),
                         ["energymap_heating"]),
    "cooling_demand": (f"SUM({COOLING_ANNUAL_EXPR})",
                       field("cooling_demand", "quantitative", "Kältebedarf (Platzhalter)", "kWh/a"),
                       ["cooling_placeholder"]),
    "cooling_specific": (f"SUM({COOLING_ANNUAL_EXPR}) / NULLIF(SUM({COOLING_AREA_EXPR}), 0)",
                         field("cooling_specific", "quantitative", "spez. Kältebedarf (Platzhalter)", "kWh/(m²·a)"),
                         ["cooling_placeholder"]),
    "cooling_capacity": (f"SUM({COOLING_CAPACITY_EXPR})",
                         field("cooling_capacity", "quantitative", "Kälteleistung (Platzhalter)", "kW"),
                         ["cooling_placeholder"]),
    "heated_area": ('SUM(pb."Heated area")', field("heated_area", "quantitative", "beheizte Fläche", "m²"),
                    ["building_params"]),
    "building_count": ("COUNT(*)", field("building_count", "quantitative", "Anzahl Gebäude", "Gebäude"),
                       ["footprints"]),
}

_FILTER_PROPS = {
    "usage_zone_type": {"type": "string", "description": "Restrict to one usage type (same values and group words as find_buildings)."},
    "postcodes": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these Berlin postcodes; overrides the map view."},
    "min_year_built": {"type": "integer", "description": "Earliest year of construction"},
    "max_year_built": {"type": "integer", "description": "Latest year of construction"},
    "districts": {
        "type": "array", "items": {"type": "string"},
        "description": "Restrict to these Berlin districts (partial names, e.g. ['Neukölln', 'Mitte']). "
                       "Use this to compare districts; it overrides the map view.",
    },
    "scope": {
        "type": "string", "enum": ["view", "berlin"],
        "description": "'view' (default): the drawn area or visible map area. 'berlin': all of Berlin, for "
                       "questions about the whole city.",
    },
}

AGGREGATE_BUILDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": list(METRICS),
                   "description": "heating_demand (annual kWh, sum), heating_specific (kWh per m² and year, "
                                  "area-weighted), cooling_demand (annual kWh, generic placeholder), cooling_specific "
                                  "(kWh per m² and year, placeholder), cooling_capacity (kW, placeholder), heated_area, "
                                  "building_count"},
        "group_by": {"type": "array", "items": {"type": "string", "enum": list(GROUPS)}, "minItems": 0,
                     "maxItems": 2,
                     "description": "Zero, one or two grouping levels, outer first, e.g. ['district', 'usage_type']. "
                                    "Leave empty for one single number ('wie viele', 'wie hoch insgesamt')."},
        "share_of_total": {"type": "boolean",
                           "description": "With no grouping and a usage or year filter: also return the share of the "
                                          "total of the same area ('welchen Anteil haben Wohngebäude')."},
        "top_n": {"type": "integer", "description": "Keep only the largest n groups (default: all)"},
        **_FILTER_PROPS,
    },
    "required": ["metric"],
}

DENSITY_GRID_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": ["cooling_demand", "cooling_capacity", "heat_cooling_overlap"],
                   "description": "cooling_demand (MWh per hectare and year, placeholder), cooling_capacity (kW per "
                                  "hectare, placeholder), heat_cooling_overlap (the smaller of heating and cooling "
                                  "density: where both are high)"},
        "cell_m": {"type": "integer", "description": "Cell size in meters, 100-1000, default 250"},
        "top_n": {"type": "integer", "description": "Keep the n densest cells, default 400, max 1500"},
        **{k: _FILTER_PROPS[k] for k in ("usage_zone_type", "districts", "scope")},
    },
    "required": ["metric"],
}

AGGREGATE_CHILLERS_SCHEMA = {
    "type": "object",
    "properties": {
        "chiller_group": {"type": "string", "enum": ["cooling_tower", "air_cooled", "hybrid"],
                          "description": "cooling_tower (Kühlturm, Zellenkühlturm), air_cooled (Rückkühler), "
                                         "hybrid (suspected). Omit for all."},
        "count": {"type": "string", "enum": ["objects", "buildings"],
                  "description": "Count detected objects (default) or the buildings carrying at least one."},
        "group_by": {"type": "string", "enum": ["district", "chiller_class"], "description": "Optional grouping"},
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
    },
    "required": [],
}

REPORT_MISSING_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "enum": ["climate_scenarios", "measures", "indoor_temperature", "outdoor_heat",
                                             "electricity", "pv", "cooling_networks", "chiller_capacity",
                                             "other_city", "ownership", "building_physics", "data_centers"],
                  "description": "Which data the question needs and the map does not have."},
        "place": {"type": "string", "description": "With other_city: the place asked about"},
    },
    "required": ["topic"],
}

MONTHLY_PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "group_by": {"type": "string", "enum": ["usage_type", "district"],
                     "description": "Optional: one line per group instead of one total line."},
        "top_n": {"type": "integer", "description": "With group_by: keep the n groups with the largest demand"},
        **_FILTER_PROPS,
    },
    "required": [],
}

TOOLS = [
    {
        "name": "aggregate_buildings",
        "description": (
            "Summarize buildings, in groups or as one single number: total or specific heating and cooling demand, "
            "required cooling capacity, heated area or number of buildings per usage type, district, postcode and/or "
            "construction period. Use this for questions about counts, totals, distributions, shares, rankings and "
            "comparisons between groups or districts -- not for finding individual buildings (use find_buildings)."
        ),
        "input_schema": AGGREGATE_BUILDINGS_SCHEMA,
    },
    {
        "name": "monthly_profile",
        "description": (
            "Cooling demand per month (January to December), optionally one series per usage type or district. "
            "Use this for questions about the course of the year, seasons or peak months. Only cooling has "
            "monthly values; heating demand exists only as an annual value."
        ),
        "input_schema": MONTHLY_PROFILE_SCHEMA,
    },
    {
        "name": "density_grid",
        "description": (
            "Density on a regular grid, shown on the map: where cooling demand or capacity is concentrated, or where "
            "heating and cooling demand overlap. Use for hotspots, Dichte, and the suitability of cooling networks "
            "or combined heating and cooling supply."
        ),
        "input_schema": DENSITY_GRID_SCHEMA,
    },
    {
        "name": "aggregate_chillers",
        "description": (
            "Count rooftop cooling equipment detected from aerial imagery, or the buildings carrying it, optionally "
            "per district or class. Use for 'wie viele Kühltürme / Gebäude mit Kühlanlage'. To show them on the map "
            "use find_chillers. Capacity per unit is not available."
        ),
        "input_schema": AGGREGATE_CHILLERS_SCHEMA,
    },
    {
        "name": "report_missing_data",
        "description": (
            "Call this when answering needs data the map does not have yet: climate scenarios or future years, "
            "effects of measures, indoor temperatures or overheating, outdoor heat stress, electricity demand, PV, "
            "cooling networks, chiller capacity, cities other than Berlin, ownership (municipal buildings), U-values or "
            "retrofit state, data centres. It returns what is missing and who supplies it. Never estimate these yourself."
        ),
        "input_schema": REPORT_MISSING_SCHEMA,
    },
]

MISSING = {
    "climate_scenarios": ("Klimaszenarien und Zukunftsjahre", "kommen aus der Simulation im Projekt"),
    "measures": ("Wirkungen von Maßnahmen und Sanierungen auf den Kältebedarf", "kommen aus der Simulation im Projekt"),
    "indoor_temperature": ("Innenraumtemperaturen und Überhitzungsstunden", "kommen aus der Simulation im Projekt"),
    "outdoor_heat": ("Angaben zur Wärmebelastung im Außenraum", "die Einbindung der HOSTRADA-Klimadaten ist geplant"),
    "electricity": ("Strombedarf und Lastgänge", "sind noch nicht im Datenbestand"),
    "pv": ("Angaben zum PV-Potenzial", "sind noch nicht im Datenbestand"),
    "cooling_networks": ("Angaben zu Kältenetzen", "sind noch nicht im Datenbestand"),
    "chiller_capacity": ("Kälteleistungen je erkannter Anlage", "Standorte und Typen sind da, die Leistung kommt vom KIT"),
    "other_city": ("Gebäudedaten außerhalb Berlins", "Stuttgart und Korbach folgen im Projekt, weitere Städte sind nicht vorgesehen"),
    "ownership": ("Angaben zum Eigentum (z. B. kommunale Liegenschaften)", "sind noch nicht im Datenbestand"),
    "building_physics": ("U-Werte und Sanierungsstand je Gebäude", "sind noch nicht im Datenbestand"),
    "data_centers": ("Rechenzentren als eigene Nutzungsart", "sind noch nicht im Datenbestand"),
}


def _area_sql(db_params: dict, bbox: str | None, polygon: dict | None, districts: list[str] | None,
              scope: str | None, need_district: bool, postcodes: list[str] | None = None) -> tuple[str, str]:
    """(extra FROM/JOIN, extra WHERE) restricting buildings to the requested area.

    District membership comes from the statistical block the building mostly lies in (emc.building.
    mostly_in_block -> emc.block.in_district), not from a spatial test: 97.7 % of buildings have a block, and
    on a 3,000-building sample it agreed with the point-in-district-polygon result in every case. Berlin-wide
    grouping by district takes ~2 s this way instead of over a minute. Buildings without a block (mostly
    outside Berlin) are left out of district questions."""
    join, where = "", []
    if need_district or (districts and not postcodes):
        join = """
            JOIN emc.block blk ON blk.blk_id = b.mostly_in_block
            JOIN emc.district d ON d.dist_id = blk.in_district"""
    if postcodes:
        db_params["postcodes"] = [int(p) for p in postcodes if str(p).strip().isdigit()]
        where.append("e.postcode = ANY(:postcodes)")
    elif districts:
        ors = []
        for k, name in enumerate(districts):
            db_params[f"district_{k}"] = f"%{name}%"
            ors.append(f"d.name ILIKE :district_{k}")
        where.append(f"({' OR '.join(ors)})")
    elif polygon:
        where.append(f"ST_Intersects(b.geom, {_build_search_geom_expr(polygon, None, db_params)})")
    elif scope != "berlin" and bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
            where.append("b.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)")
            db_params.update({"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat})
        except ValueError:
            pass
    return join, " ".join(f"AND {w}" for w in where)


def _filter_sql(params: dict, usage_zone_type, min_year_built, max_year_built) -> str:
    cond = []
    if usage_zone_type:
        cond.append(usage_condition(usage_zone_type, params))
    if min_year_built is not None:
        cond.append(f"{_YEAR} >= :min_year_built")
        params["min_year_built"] = min_year_built
    if max_year_built is not None:
        cond.append(f"{_YEAR} <= :max_year_built")
        params["max_year_built"] = max_year_built
    return " ".join(f"AND {c}" for c in cond)


_BASE = """
    FROM emc.building b
    LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
    LEFT JOIN emp.emblive e ON e.uuid = b.uuid
    LEFT JOIN core_bldg.usage_cooling_profile ucp ON ucp.usage_zone_type = pb."PrimaryUsageZoneType"
"""


def _single_value(db, metric, agg_sql, metric_field, metric_sources, join, where_area, where_filter, params,
                  share_of_total, query, notes) -> dict:
    """No grouping: one number for the whole area, optionally with its share of the unfiltered total."""
    r = db.execute(text(f"SELECT {agg_sql} AS value, COUNT(*) AS n {_BASE} {join} WHERE 1=1 {where_area} {where_filter}"),
                   params).one()
    if r.n == 0:
        return frame("aggregate", [metric_field], [], ["building_params"], total=0, query=query,
                     notes=["Keine Gebäude in diesem Gebiet im Datenbestand. Vorhanden ist derzeit nur Berlin."])
    value = round(float(r.value), 1) if r.value is not None else None
    fields, row = [metric_field], {metric: value}
    if share_of_total and where_filter:
        area_params = {k: v for k, v in params.items() if not k.startswith(("usage_", "min_year", "max_year"))}
        total = db.execute(text(f"SELECT {agg_sql} AS value {_BASE} {join} WHERE 1=1 {where_area}"), area_params).one().value
        if total:
            fields.insert(0, field("share_percent", "quantitative", f"Anteil an {metric_field['label']} gesamt", "%"))
            row["share_percent"] = round(100 * float(r.value or 0) / float(total), 1)
    if metric != "building_count":
        row["building_count"] = r.n
        fields.append(METRICS["building_count"][1])
    return frame("aggregate", fields, [row], ["building_params", *metric_sources], total=1, query=query, notes=notes)


def aggregate_buildings(db: Session, bbox: str | None, polygon: dict | None = None, *, metric: str,
                        group_by: list[str] | None = None, top_n: int | None = None, usage_zone_type: str | None = None,
                        min_year_built: int | None = None, max_year_built: int | None = None,
                        districts: list[str] | None = None, scope: str | None = None,
                        postcodes: list[str] | None = None, share_of_total: bool | None = None) -> dict:
    if metric not in METRICS:
        return {"error": f"unknown metric {metric}"}
    group_by = [g for g in (group_by or []) if g in GROUPS][:2]
    agg_sql, metric_field, metric_sources = METRICS[metric]

    params: dict = {}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, "district" in group_by, postcodes)
    where_filter = _filter_sql(params, usage_zone_type, min_year_built, max_year_built)
    query = {"metric": metric, "group_by": group_by, "districts": districts, "scope": scope, "postcodes": postcodes,
             "usage_zone_type": usage_zone_type, "min_year_built": min_year_built, "max_year_built": max_year_built,
             "top_n": top_n, "share_of_total": share_of_total}
    if not group_by:
        notes = (["Kältewerte sind generische Platzhalter je Nutzungsart, keine Simulation."]
                 if metric.startswith("cooling") else [])
        return _single_value(db, metric, agg_sql, metric_field, metric_sources, join, where_area, where_filter, params,
                             share_of_total, query, notes)
    keys = [GROUPS[g][0] for g in group_by]
    select_keys = ", ".join(f"{k} AS g{n}" for n, k in enumerate(keys))
    group_keys = ", ".join(f"g{n}" for n in range(len(keys)))
    rows = db.execute(text(f"""
        SELECT {select_keys}, {agg_sql} AS value, COUNT(*) AS n
        {_BASE} {join}
        WHERE 1=1 {where_area} {where_filter}
        GROUP BY {group_keys}
        ORDER BY value DESC NULLS LAST
    """), params).all()

    out = []
    for r in rows:
        row = {g: (getattr(r, f"g{n}") or "unbekannt") for n, g in enumerate(group_by)}
        row[metric] = round(float(r.value), 1) if r.value is not None else None
        row["building_count"] = r.n
        out.append(row)
    total_groups = len(out)
    if top_n and len(group_by) == 1:
        out = out[: int(top_n)]
    if group_by == ["construction_period"]:
        order = [p[0] for p in PERIODS] + ["unbekannt"]
        out.sort(key=lambda r: order.index(r["construction_period"]))

    fields = [GROUPS[g][1] for g in group_by] + [metric_field]
    if metric != "building_count":
        fields.append(METRICS["building_count"][1])
    sources = ["building_params", *metric_sources] + (["districts"] if (districts or "district" in group_by) else [])
    notes = []
    if not out:
        notes.append("Keine Gebäude in diesem Gebiet im Datenbestand. Vorhanden ist derzeit nur Berlin.")
    elif metric.startswith("cooling"):
        notes.append("Kältewerte sind generische Platzhalter je Nutzungsart, keine Simulation.")
    return frame("aggregate", fields, out, sources, hierarchy=group_by if len(group_by) > 1 else None,
                 total=total_groups, query=query, notes=notes)


def monthly_profile(db: Session, bbox: str | None, polygon: dict | None = None, *, group_by: str | None = None,
                    top_n: int | None = None, usage_zone_type: str | None = None,
                    min_year_built: int | None = None, max_year_built: int | None = None,
                    districts: list[str] | None = None, scope: str | None = None) -> dict:
    params: dict = {}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, group_by == "district")
    where_filter = _filter_sql(params, usage_zone_type, min_year_built, max_year_built)
    gkey = GROUPS[group_by][0] if group_by in ("usage_type", "district") else "'gesamt'"
    months = ", ".join(f"SUM({COOLING_ANNUAL_EXPR} * COALESCE(ucp.pct_{m:02d}, 0) / 100) AS m{m}" for m in range(1, 13))
    rows = db.execute(text(f"""
        SELECT {gkey} AS g, {months}, SUM({COOLING_ANNUAL_EXPR}) AS annual
        {_BASE} {join}
        WHERE 1=1 {where_area} {where_filter}
        GROUP BY 1
        ORDER BY annual DESC NULLS LAST
    """), params).all()
    if top_n:
        rows = rows[: int(top_n)]

    out = []
    for r in rows:
        for m in range(1, 13):
            v = getattr(r, f"m{m}")
            row = {"month": m, "month_label": MONTHS[m - 1], "cooling_demand": round(float(v), 1) if v is not None else None}
            if group_by:
                row[group_by] = r.g or "unbekannt"
            out.append(row)

    fields = [field("month", "temporal", "Monat")]
    if group_by:
        fields.append(GROUPS[group_by][1])
    fields.append(field("cooling_demand", "quantitative", "Kältebedarf (Platzhalter)", "kWh/Monat"))
    return frame("series", fields, out, ["building_params", "cooling_placeholder"]
                 + (["districts"] if (districts or group_by == "district") else []),
                 total=len(out),
                 query={"group_by": group_by, "districts": districts, "scope": scope,
                        "usage_zone_type": usage_zone_type, "min_year_built": min_year_built,
                        "max_year_built": max_year_built, "top_n": top_n},
                 notes=["Kältebedarf ist ein generischer Platzhalter; die Monatsverteilung ist für alle "
                        "Nutzungsarten gleich (Mai–September)."])


def density_grid(db: Session, bbox: str | None, polygon: dict | None = None, *, metric: str = "cooling_demand",
                 cell_m: int | None = None, top_n: int | None = None, usage_zone_type: str | None = None,
                 districts: list[str] | None = None, scope: str | None = None) -> dict:
    cell = max(100, min(int(cell_m or 250), 1000))
    top = max(1, min(int(top_n or 400), 1500))
    params: dict = {"cell": cell, "top": top}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False)
    where_filter = _filter_sql(params, usage_zone_type, None, None)
    per_ha = 10000.0 / (cell * cell)
    cool = f"SUM({COOLING_ANNUAL_EXPR}) / 1000 * {per_ha}"
    heat = f"SUM(COALESCE(e.cons_c2r2, 0)) / 1000 * {per_ha}"
    expr, label, unit, sources = {
        "cooling_demand": (cool, "Kältedichte (Platzhalter)", "MWh/(ha·a)", ["cooling_placeholder"]),
        "cooling_capacity": (f"SUM({COOLING_CAPACITY_EXPR}) * {per_ha}", "Kälteleistungsdichte (Platzhalter)", "kW/ha",
                             ["cooling_placeholder"]),
        "heat_cooling_overlap": (f"LEAST({cool}, {heat})", "Überlagerung Wärme und Kälte (kleinerer Wert)", "MWh/(ha·a)",
                                 ["cooling_placeholder", "energymap_heating"]),
    }.get(metric, (None, None, None, None))
    if expr is None:
        return {"error": f"unknown metric {metric}"}
    rows = db.execute(text(f"""
        WITH cells AS (
            SELECT ROUND(ST_X(p.pt) / :cell) * :cell AS x, ROUND(ST_Y(p.pt) / :cell) * :cell AS y,
                   {expr} AS value, COUNT(*) AS n
            {_BASE} {join}
            CROSS JOIN LATERAL (SELECT ST_PointOnSurface(b.geom) AS pt) p
            WHERE 1=1 {where_area} {where_filter}
            GROUP BY 1, 2
            ORDER BY value DESC NULLS LAST
            LIMIT :top
        )
        SELECT value, n,
               ST_AsGeoJSON(ST_Transform(ST_MakeEnvelope(x - :cell / 2.0, y - :cell / 2.0, x + :cell / 2.0, y + :cell / 2.0, 25833), 4326), 6) AS geom,
               ST_X(ST_Transform(ST_SetSRID(ST_MakePoint(x, y), 25833), 4326)) AS lon,
               ST_Y(ST_Transform(ST_SetSRID(ST_MakePoint(x, y), 25833), 4326)) AS lat
        FROM cells WHERE value > 0
    """), params).all()
    features, out = [], []
    for k, r in enumerate(rows):
        v = round(float(r.value), 1)
        features.append({"type": "Feature", "geometry": json.loads(r.geom),
                         "properties": {"cell": k + 1, "value": v, "building_count": r.n}})
        out.append({"rank": k + 1, "value": v, "building_count": r.n, "lon": round(r.lon, 5), "lat": round(r.lat, 5)})
    fr = frame("features",
               [field("rank", "ordinal", "Rang"), field("value", "quantitative", label, unit),
                field("building_count", "quantitative", "Anzahl Gebäude", "Gebäude"),
                field("lon", "ordinal", "Länge"), field("lat", "ordinal", "Breite")],
               out, ["building_params", *sources], total=len(out),
               query={"metric": metric, "cell_m": cell, "districts": districts, "scope": scope,
                      "usage_zone_type": usage_zone_type},
               notes=[f"Rasterzellen {cell} m × {cell} m, gezeigt werden die dichtesten {len(out)}. Kältewerte sind "
                      "generische Platzhalter, keine Simulation. Eine echte Liniendichte braucht das Straßennetz und fehlt noch."])
    fr["geo"] = True
    return {"layer": "cells", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr,
            "summary_for_llm": {"cells": len(out), "highest": out[0] if out else None, "unit": unit, "metric": label}}


CHILLER_GROUPS = {"cooling_tower": "c.class LIKE 'CT_%'", "air_cooled": "c.class = 'ACC_Large'",
                  "hybrid": "c.class = 'Hybrid_supposed'"}


def aggregate_chillers(db: Session, bbox: str | None, polygon: dict | None = None, *, chiller_group: str | None = None,
                       count: str | None = None, group_by: str | None = None, districts: list[str] | None = None,
                       scope: str | None = None) -> dict:
    params: dict = {}
    where, join = [], ""
    if chiller_group in CHILLER_GROUPS:
        where.append(CHILLER_GROUPS[chiller_group])
    if districts or group_by == "district":
        join += " JOIN emc.district d ON ST_Intersects(d.geom, ST_PointOnSurface(c.geom))"
    if districts:
        ors = []
        for k, name in enumerate(districts):
            params[f"district_{k}"] = f"%{name}%"
            ors.append(f"d.name ILIKE :district_{k}")
        where.append(f"({' OR '.join(ors)})")
    elif polygon:
        where.append(f"ST_Intersects(c.geom, {_build_search_geom_expr(polygon, None, params)})")
    elif scope != "berlin" and bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
            where.append("c.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)")
            params.update({"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat})
        except ValueError:
            pass
    by_building = count == "buildings"
    if by_building:
        join += " JOIN emc.building b ON ST_Intersects(b.geom, ST_PointOnSurface(c.geom))"
    value = "COUNT(DISTINCT b.uuid)" if by_building else "COUNT(*)"
    key = {"district": "d.name", "chiller_class": "c.class"}.get(group_by or "")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f'FROM core_building."Chillers" c {join} {where_sql}'
    metric = field("chiller_count", "quantitative", "Gebäude mit Kühlanlage" if by_building else "Erkannte Kühlanlagen",
                   "Gebäude" if by_building else "Anlagen")
    query = {"chiller_group": chiller_group, "count": count, "group_by": group_by, "districts": districts, "scope": scope}
    notes = ["Aus Luftbildern erkannt. Die Kälteleistung je Anlage liegt noch nicht vor (kommt vom KIT)."]
    if key:
        rows = db.execute(text(f"SELECT {key} AS g, {value} AS v {sql} GROUP BY 1 ORDER BY v DESC"), params).all()
        return frame("aggregate", [field(group_by, "nominal", "Bezirk" if group_by == "district" else "Klasse"), metric],
                     [{group_by: r.g, "chiller_count": r.v} for r in rows], ["chillers", "districts"], query=query,
                     notes=notes)
    v = db.execute(text(f"SELECT {value} AS v {sql}"), params).one().v
    return frame("aggregate", [metric], [{"chiller_count": v}], ["chillers"], total=1, query=query, notes=notes)


def report_missing_data(db: Session, bbox: str | None, polygon: dict | None = None, *, topic: str,
                        place: str | None = None) -> dict:
    what, why = MISSING.get(topic, ("Diese Daten", "sind noch nicht im Datenbestand"))
    if topic == "other_city" and place:
        what = f"Gebäudedaten für {place}"
    return frame("none", [], [], [], total=0, query={"topic": topic, "place": place},
                 notes=[f"Fehlt noch: {what}. {why[0].upper()}{why[1:]}."])


def execute(name: str, tool_input: dict, db: Session, bbox: str | None, polygon: dict | None) -> dict | None:
    """Run one of this module's tools, or None if the name isn't one of them."""
    if name == "aggregate_buildings":
        return aggregate_buildings(db, bbox, polygon, **tool_input)
    if name == "monthly_profile":
        return monthly_profile(db, bbox, polygon, **tool_input)
    if name == "density_grid":
        return density_grid(db, bbox, polygon, **tool_input)
    if name == "aggregate_chillers":
        return aggregate_chillers(db, bbox, polygon, **tool_input)
    if name == "report_missing_data":
        return report_missing_data(db, bbox, polygon, **tool_input)
    return None
