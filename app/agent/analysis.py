"""Aggregating tools for the chat agent: questions that are answered with groups or a time series rather than
with individual buildings ("heating demand by usage type", "cooling demand over the year", "compare Neukölln
and Mitte"). Read-only and parameterized like the find_* tools in tools.py -- no free SQL.

Both return result frames (frames.py), so the presentation chooser can turn them into a bar, line or sunburst
chart, a table or a sentence.
"""
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import cooling
from .frames import field, frame
from .tools import (BUILDING_FUNCTION_PROP, EXTRA_FILTER_PROPS,
                    CHILLER_CAPACITY_NOTE, COOLING_ACTUAL_NOTE, KWW_JOINS, OWNER_EXPR, extra_conditions, extra_sources,
                    PROCESS_HEAT_PROP, _build_search_geom_expr, building_function_condition,
                    process_heat_condition, usage_condition)

MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

COOLING_AREA_EXPR = f"CASE WHEN {cooling.ANNUAL} IS NOT NULL THEN {cooling.AREA} END"

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
    "building_function": ("kg.gfk_name", field("building_function", "nominal", "Gebäudefunktion (ALKIS)")),
    "plot_owner": (OWNER_EXPR, field("plot_owner", "nominal", "Grundstückseigentum")),
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
    "cooling_demand": (f"SUM({cooling.ANNUAL})", field("cooling_demand", "quantitative", "Kältebedarf", "kWh/a"),
                       ["cooling_mockup"]),
    "cooling_specific": (f"SUM({cooling.ANNUAL}) / NULLIF(SUM({COOLING_AREA_EXPR}), 0)",
                         field("cooling_specific", "quantitative", "spez. Kältebedarf", "kWh/(m²·a)"),
                         ["cooling_mockup"]),
    "cooling_capacity": (f"SUM({cooling.CAPACITY})",
                         field("cooling_capacity", "quantitative", "benötigte Kälteleistung", "kW"),
                         ["cooling_mockup"]),
    "kww_heat_demand": ("SUM(kh.heat_dhw_kwh_a)",
                        field("kww_heat_demand", "quantitative", "Wärmebedarf KWW (Heizung + Warmwasser)", "kWh/a"),
                        ["kww_heat"]),
    "pv_potential_kwp": ("SUM(pvp.potential_kwp)", field("pv_potential_kwp", "quantitative", "PV-Potenzial Dach", "kWp"),
                         ["pv_potential"]),
    "pv_installed_kwp": ("SUM(pvi.installed_kwp)",
                         field("pv_installed_kwp", "quantitative", "PV installiert (ab 30 kWp / öffentlich)", "kWp"),
                         ["pv_installed"]),
    "cooling_actual": ("SUM(cact.cooling_kwh_mean)",
                       field("cooling_actual", "quantitative", "Kälteverbrauch (aus installierter Leistung)", "kWh/a"),
                       ["chillers", "chiller_estimate"]),
    "electricity_cooling": ("SUM(cact.electricity_kwh_mean)",
                            field("electricity_cooling", "quantitative", "Kältestrom (Schätzung, Mittel)", "kWh/a"),
                            ["chillers", "chiller_estimate"]),
    "chiller_capacity": ("SUM(cact.capacity_kw_mean)",
                         field("chiller_capacity", "quantitative", "Kälteleistung erkannter Anlagen (geschätzt)", "kW"),
                         ["chillers", "chiller_estimate"]),
    "heated_area": ('SUM(pb."Heated area")', field("heated_area", "quantitative", "beheizte Fläche", "m²"),
                    ["building_params"]),
    "building_count": ("COUNT(*)", field("building_count", "quantitative", "Anzahl Gebäude", "Gebäude"),
                       ["footprints"]),
}

# Metrics from detected chillers only count buildings that have such a unit.
ACTUAL_METRICS = {"cooling_actual", "electricity_cooling", "chiller_capacity"}
# min-max bands: of the chiller estimates, and of the cooling demand assumptions
METRIC_BANDS = {
    "cooling_demand": (f"SUM({cooling.ANNUAL_MIN})", f"SUM({cooling.ANNUAL_MAX})"),
    "cooling_actual": ("SUM(cact.cooling_kwh_min)", "SUM(cact.cooling_kwh_max)"),
    "electricity_cooling": ("SUM(cact.electricity_kwh_min)", "SUM(cact.electricity_kwh_max)"),
    "chiller_capacity": ("SUM(cact.capacity_kw_min)", "SUM(cact.capacity_kw_max)"),
}


CHECK_SQL = ", SUM(cact.cooling_kwh_mean) AS act, COUNT(cact.uuid) AS n_act"
CHECK_FIELDS = [field("cooling_actual", "quantitative", "Kälteverbrauch erkannter Anlagen (Abgleich)", "kWh/a"),
                field("buildings_with_chiller", "quantitative", "Gebäude mit erkannter Anlage", "Gebäude")]


def _check_values(r) -> dict:
    return {"cooling_actual": round(float(r.act), 1) if r.act is not None else None,
            "buildings_with_chiller": r.n_act}


def _band_fields(metric: str, metric_field: dict) -> list[dict]:
    return [field(f"{metric}_min", "quantitative", f"{metric_field['label']} – min", metric_field.get("unit")),
            field(f"{metric}_max", "quantitative", f"{metric_field['label']} – max", metric_field.get("unit"))]


_FILTER_PROPS = {
    "usage_zone_type": {"type": "string", "description": "Restrict to one usage type (same values and group words as find_buildings)."},
    "building_function": BUILDING_FUNCTION_PROP,
    "process_heat": PROCESS_HEAT_PROP,
    **EXTRA_FILTER_PROPS,
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
                   "description": "cooling_actual (estimated actual annual cooling of detected cooling units, kWh, with min/max band), "
                                  "electricity_cooling (their estimated electricity use, kWh, with band), chiller_capacity "
                                  "(estimated installed capacity, kW), pv_potential_kwp (rooftop PV potential, solar cadastre), pv_installed_kwp (registered PV > 30 kWp "
                                  "or public sector; small private systems missing), kww_heat_demand (annual kWh, heating + hot water after the KWW catalogue, gross floor area "
                                  "x benchmark), heating_demand (annual kWh, sum), heating_specific (kWh per m² and year, "
                                  "area-weighted), cooling_demand (annual kWh, assumption per building function and "
                                  "age class in a scenario; comes with the consumption of detected chillers as cross-check), "
                                  "cooling_specific (kWh per m² and year), cooling_capacity (required kW = demand / "
                                  "full-load hours), heated_area, building_count"},
        "group_by": {"type": "array", "items": {"type": "string", "enum": list(GROUPS)}, "minItems": 0,
                     "maxItems": 2,
                     "description": "Zero, one or two grouping levels, outer first, e.g. ['district', 'usage_type']. "
                                    "building_function = official ALKIS building function (finer than usage_type), "
                                    "plot_owner = state ownership of the plot. "
                                    "Leave empty for one single number ('wie viele', 'wie hoch insgesamt')."},
        "share_of_total": {"type": "boolean",
                           "description": "With no grouping and a usage or year filter: also return the share of the "
                                          "total of the same area ('welchen Anteil haben Wohngebäude')."},
        "top_n": {"type": "integer", "description": "Keep only the largest n groups (default: all)"},
        **_FILTER_PROPS,
        **cooling.SCENARIO_PROPS,
    },
    "required": ["metric"],
}

DENSITY_GRID_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": ["cooling_demand", "cooling_capacity", "heat_cooling_overlap"],
                   "description": "cooling_demand (MWh per hectare and year), cooling_capacity (required kW per "
                                  "hectare), heat_cooling_overlap (the smaller of heating and cooling "
                                  "density: where both are high)"},
        "cell_m": {"type": "integer", "description": "Cell size in meters, 100-1000, default 250"},
        "top_n": {"type": "integer", "description": "Keep the n densest cells, default 400, max 1500"},
        **{k: _FILTER_PROPS[k] for k in ("usage_zone_type", "building_function", "process_heat", "districts", "scope",
                                         *EXTRA_FILTER_PROPS)},
        **cooling.SCENARIO_PROPS,
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
        **cooling.SCENARIO_PROPS,
    },
    "required": [],
}

BUILDING_SCENARIO_SCHEMA = {
    "type": "object",
    "properties": {
        "compare_measures": {"type": "array", "items": {"type": "string", "enum": cooling.MEASURES},
                             "description": "Measures whose effect is asked ('wie viel weniger, wenn ...'): "
                                            "verschattung_aussen (external window shading), lueftung_nutzer (ventilation "
                                            "by users), nachtlueftung_auto (automatic night ventilation)."},
        **{k: v for k, v in cooling.SCENARIO_PROPS.items() if k != "measures"},
        "top_n": {"type": "integer", "description": "Number of buildings with the highest cooling demand, default 10, max 500"},
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
    },
    "required": ["compare_measures"],
}

LINE_DENSITY_SCENARIOS = ["standard", "2025", "2045_gering", "2045_mittel", "2045_stark", "2045_stark_massnahmen"]

LINE_DENSITY_SCHEMA = {
    "type": "object",
    "properties": {
        "carrier": {"type": "string", "enum": ["heat", "cooling"],
                    "description": "heat (Wärmeliniendichte, Wärmenetz) or cooling (Kälteliniendichte, Kältenetz)"},
        "scenario": {"type": "string", "enum": LINE_DENSITY_SCENARIOS,
                     "description": "standard (default) = 2030, medium climate change, external window shading, "
                                    "setpoint per building function. 2025 = today without climate change. 2045_gering/_mittel/_stark = 2045 with low/medium/strong "
                                    "climate change; 2045_stark_massnahmen = strong climate change with external "
                                    "shading, user ventilation, automatic night ventilation and setpoint 26 °C. "
                                    "Only when the question names a future year, climate change or measures."},
        "group_by": {"type": "string", "enum": ["segment", "street"],
                     "description": "segment = single street sections (default, finest); street = whole streets "
                                    "per district (demand summed over all sections / total length), for 'welche Straße'."},
        "basis": {"type": "string", "enum": ["street", "trasse"],
                  "description": "trasse = per metre of main line plus all house connection lines (default: a "
                                 "network only pays off with the buildings connected); street = per metre of street "
                                 "section only, like the Berlin heat plan maps (use when compared with those)"},
        "min_length_m": {"type": "number",
                         "description": "Leave out sections shorter than this (m). Use 50 for 'wo lohnt sich ein Netz', "
                                        "so very short sections don't dominate (with group_by street: 200)."},
        "min_buildings": {"type": "integer",
                          "description": "Leave out sections/streets with fewer connected buildings. Use 5 for "
                                         "'wo lohnt sich ein Netz', so a single large building is not called a network."},
        "top_n": {"type": "integer", "description": "Keep the n highest, default 300, max 2000"},
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
    },
    "required": ["carrier"],
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
            "heating and cooling demand overlap. Use for hotspots, Dichte (per area) and combined heating and cooling supply. "
            "For network suitability along streets and Liniendichte use line_density instead."
        ),
        "input_schema": DENSITY_GRID_SCHEMA,
    },
    {
        "name": "building_scenario_demand",
        "description": (
            "Effect of measures on cooling demand: the buildings with the highest cooling demand, shown on the map, "
            "each with demand without and with the measures (external window shading, user ventilation, automatic "
            "night ventilation) and the saving, with range. Use for 'wie viel weniger Kältebedarf, wenn ...'. "
            "Assumptions until the project partners simulate, not a simulation."
        ),
        "input_schema": BUILDING_SCENARIO_SCHEMA,
    },
    {
        "name": "line_density",
        "description": (
            "Heat or cooling line density (Wärme-/Kälteliniendichte, kWh per metre and year) along the potential "
            "network on Berlin's streets, shown on the map as coloured street sections with their house connection lines. Use for 'wo ist die "
            "Kälteliniendichte / Wärmeliniendichte am höchsten', 'wo lohnt sich ein Kältenetz / Wärmenetz am meisten', "
            "network suitability along streets. Higher line density = more demand per metre of pipe = more favourable "
            "for a network. Heat = KWW (heating + hot water); cooling = placeholder assumption, not a simulation. "
            "Also has 2045 climate scenarios and measures, but only for this line density."
        ),
        "input_schema": LINE_DENSITY_SCHEMA,
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
            "Call this when answering needs data the map does not have yet: simulated (not assumed) climate "
            "scenarios or measure effects, indoor temperatures or overheating, outdoor heat stress, electricity demand, PV, "
            "cooling networks, chiller capacity, cities other than Berlin, ownership (municipal buildings), U-values or "
            "retrofit state, data centres. It returns what is missing and who supplies it. Never estimate these yourself."
        ),
        "input_schema": REPORT_MISSING_SCHEMA,
    },
]

MISSING = {
    "climate_scenarios": ("Simulierte Klimaszenarien und Zukunftsjahre",
                          "kommen aus der Simulation im Projekt; Annahmen (Mockup) je Gebäude gibt es schon (building_scenario_demand)"),
    "measures": ("Simulierte Wirkungen von Maßnahmen und Sanierungen auf den Kältebedarf",
                 "kommen aus der Simulation im Projekt; Annahmen (Mockup) für Verschattung und Lüftung gibt es schon (building_scenario_demand)"),
    "indoor_temperature": ("Innenraumtemperaturen und Überhitzungsstunden", "kommen aus der Simulation im Projekt"),
    "outdoor_heat": ("Angaben zur Wärmebelastung im Außenraum", "die Einbindung der HOSTRADA-Klimadaten ist geplant"),
    "electricity": ("Gesamtstrombedarf und Lastgänge",
                    "sind noch nicht im Datenbestand; nur der Kältestrom erkannter Anlagen ist grob geschätzt (Metrik electricity_cooling)"),
    "pv": ("PV-Erzeugung im Zeitverlauf und kleine PV-Anlagen je Gebäude",
           "Dachpotenzial (kWp) und Anlagen ab 30 kWp bzw. der öffentlichen Hand sind da (find_buildings), kleine Anlagen nur je PLZ"),
    "cooling_networks": ("Bestehende Kältenetze (Lage, Betreiber, Kapazität)",
                         "sind noch nicht im Datenbestand; die potenzielle Netzstruktur auf dem Straßennetz und Wärme-/Kälteliniendichten sind da (line_density)"),
    "chiller_capacity": ("gemessene Kälteleistungen je erkannter Anlage",
                         "es gibt die KIT-Schätzung je Anlage aus dem Luftbild (Metrik chiller_capacity, mit Spanne; find_chillers je Anlage)"),
    "other_city": ("Gebäudedaten außerhalb Berlins", "Stuttgart und Korbach folgen im Projekt, weitere Städte sind nicht vorgesehen"),
    "ownership": ("Eigentum außer dem Land Berlin (Bund, Private, Gesellschaften, Wohnungsbaugesellschaften) je Gebäude",
                  "landeseigene Grundstücke sind da (Filter owner), andere Eigentümergruppen gibt es nur als Anteil je Planungsraum"),
    "building_physics": ("U-Werte und Sanierungsstand je Gebäude", "sind noch nicht im Datenbestand"),
    "data_centers": ("Vollständige Liste und Kältebedarf der Rechenzentren",
                     "bekannte Standorte aus OpenStreetMap und PeeringDB sind da (building_function 'Rechenzentrum'), ihr Kältebedarf ist eine Annahme (Mockup)"),
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


def _filter_sql(params: dict, usage_zone_type, min_year_built, max_year_built, db: Session | None = None,
                building_function: str | None = None, process_heat: bool | None = None, **extra) -> str:
    cond = extra_conditions(params, **extra)
    if usage_zone_type:
        cond.append(usage_condition(usage_zone_type, params))
    if building_function and db is not None:
        # unknown function name -> no rows (the frame then says so) rather than silently ignoring the filter
        cond.append(building_function_condition(db, building_function, params) or "FALSE")
    if process_heat is not None:
        cond.append(process_heat_condition(process_heat))
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
    """ + cooling.COOL_JOIN + KWW_JOINS + "\n"


def _single_value(db, metric, agg_sql, metric_field, metric_sources, join, where_area, where_filter, params,
                  share_of_total, query, notes) -> dict:
    """No grouping: one number for the whole area, optionally with its share of the unfiltered total."""
    band = METRIC_BANDS.get(metric)
    band_sql = f", {band[0]} AS vmin, {band[1]} AS vmax" if band else ""
    check = metric == "cooling_demand"
    band_sql += CHECK_SQL if check else ""
    r = db.execute(text(f"SELECT {agg_sql} AS value, COUNT(*) AS n{band_sql} {_BASE} {join} WHERE 1=1 {where_area} {where_filter}"),
                   params).one()
    if r.n == 0:
        return frame("aggregate", [metric_field], [], ["building_params"], total=0, query=query,
                     notes=["Keine Gebäude in diesem Gebiet im Datenbestand. Vorhanden ist derzeit nur Berlin."])
    value = round(float(r.value), 1) if r.value is not None else None
    fields, row = [metric_field], {metric: value}
    if band:
        fields += _band_fields(metric, metric_field)
        row[f"{metric}_min"] = round(float(r.vmin), 1) if r.vmin is not None else None
        row[f"{metric}_max"] = round(float(r.vmax), 1) if r.vmax is not None else None
    if check:
        fields += CHECK_FIELDS
        row.update(_check_values(r))
    if share_of_total and where_filter:
        area_params = {k: v for k, v in params.items() if not k.startswith(("usage_", "min_year", "max_year", "bf_", "min_pv"))}
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
                        postcodes: list[str] | None = None, share_of_total: bool | None = None,
                        building_function: str | None = None, process_heat: bool | None = None,
                        climate: str | None = None, year: int | None = None, measures: list[str] | None = None,
                        setpoint: float | None = None, **extra) -> dict:
    if metric not in METRICS:
        return {"error": f"unknown metric {metric}"}
    group_by = [g for g in (group_by or []) if g in GROUPS][:2]
    agg_sql, metric_field, metric_sources = METRICS[metric]

    params: dict = {}
    sc = cooling.scenario(climate, year, measures, setpoint)
    cooling.bind(db, params, sc)
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, "district" in group_by, postcodes)
    where_filter = _filter_sql(params, usage_zone_type, min_year_built, max_year_built, db, building_function,
                               process_heat, **extra)
    metric_sources = [*metric_sources, *extra_sources(**extra)]
    band = METRIC_BANDS.get(metric)
    if metric in ACTUAL_METRICS:
        where_filter += " AND cact.uuid IS NOT NULL"
    query = {**extra, "metric": metric, "group_by": group_by, "districts": districts, "scope": scope, "postcodes": postcodes,
             "usage_zone_type": usage_zone_type, "min_year_built": min_year_built, "max_year_built": max_year_built,
             "top_n": top_n, "share_of_total": share_of_total, "building_function": building_function,
             "process_heat": process_heat, **(sc if metric.startswith("cooling_") else {})}
    cooling_notes = ([cooling.note(sc)] + ([cooling.ACTUAL_NOTE] if metric == "cooling_demand" else [])
                     + (["Benötigte Kälteleistung = Kältebedarf / Volllaststunden der Nutzung (Annahme)."]
                        if metric == "cooling_capacity" else []))
    if not group_by:
        notes = ([COOLING_ACTUAL_NOTE] if metric in ACTUAL_METRICS else
                 cooling_notes if metric.startswith("cooling_") else [])
        return _single_value(db, metric, agg_sql, metric_field, metric_sources, join, where_area, where_filter, params,
                             share_of_total, query, notes)
    keys = [GROUPS[g][0] for g in group_by]
    select_keys = ", ".join(f"{k} AS g{n}" for n, k in enumerate(keys))
    group_keys = ", ".join(f"g{n}" for n in range(len(keys)))
    band_sql = f", {band[0]} AS vmin, {band[1]} AS vmax" if band else ""
    check = metric == "cooling_demand"
    band_sql += CHECK_SQL if check else ""
    rows = db.execute(text(f"""
        SELECT {select_keys}, {agg_sql} AS value, COUNT(*) AS n{band_sql}
        {_BASE} {join}
        WHERE 1=1 {where_area} {where_filter}
        GROUP BY {group_keys}
        ORDER BY value DESC NULLS LAST
    """), params).all()

    out = []
    for r in rows:
        row = {g: (getattr(r, f"g{n}") or "unbekannt") for n, g in enumerate(group_by)}
        row[metric] = round(float(r.value), 1) if r.value is not None else None
        if band:
            row[f"{metric}_min"] = round(float(r.vmin), 1) if r.vmin is not None else None
            row[f"{metric}_max"] = round(float(r.vmax), 1) if r.vmax is not None else None
        if check:
            row.update(_check_values(r))
        row["building_count"] = r.n
        out.append(row)
    total_groups = len(out)
    if top_n and len(group_by) == 1:
        out = out[: int(top_n)]
    if group_by == ["construction_period"]:
        order = [p[0] for p in PERIODS] + ["unbekannt"]
        out.sort(key=lambda r: order.index(r["construction_period"]))

    fields = ([GROUPS[g][1] for g in group_by] + [metric_field] + (_band_fields(metric, metric_field) if band else [])
              + (CHECK_FIELDS if check else []))
    if metric != "building_count":
        fields.append(METRICS["building_count"][1])
    sources = ["building_params", *metric_sources] + (["districts"] if (districts or "district" in group_by) else [])
    notes = []
    if not out:
        notes.append("Keine Gebäude in diesem Gebiet im Datenbestand. Vorhanden ist derzeit nur Berlin.")
    elif metric in ACTUAL_METRICS:
        notes.append(COOLING_ACTUAL_NOTE)
    elif metric.startswith("cooling_"):
        notes += cooling_notes
    return frame("aggregate", fields, out, sources, hierarchy=group_by if len(group_by) > 1 else None,
                 total=total_groups, query=query, notes=notes)


def monthly_profile(db: Session, bbox: str | None, polygon: dict | None = None, *, group_by: str | None = None,
                    top_n: int | None = None, usage_zone_type: str | None = None,
                    min_year_built: int | None = None, max_year_built: int | None = None,
                    districts: list[str] | None = None, scope: str | None = None,
                    building_function: str | None = None, process_heat: bool | None = None,
                    postcodes: list[str] | None = None, climate: str | None = None, year: int | None = None,
                    measures: list[str] | None = None, setpoint: float | None = None, **extra) -> dict:
    params: dict = {}
    sc = cooling.scenario(climate, year, measures, setpoint)
    cooling.bind(db, params, sc)
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, group_by == "district", postcodes)
    where_filter = _filter_sql(params, usage_zone_type, min_year_built, max_year_built, db, building_function,
                               process_heat, **extra)
    gkey = GROUPS[group_by][0] if group_by in ("usage_type", "district") else "'gesamt'"
    # monthly shares: the generic profile per usage type (May-September), applied to the annual mock-up demand
    months = ", ".join(f"SUM({cooling.ANNUAL} * COALESCE(ucp.pct_{m:02d}, 0) / 100) AS m{m}" for m in range(1, 13))
    rows = db.execute(text(f"""
        SELECT {gkey} AS g, {months}, SUM({cooling.ANNUAL}) AS annual
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
    fields.append(field("cooling_demand", "quantitative", "Kältebedarf", "kWh/Monat"))
    return frame("series", fields, out, ["building_params", "cooling_mockup"]
                 + (["districts"] if (districts or group_by == "district") else []),
                 total=len(out),
                 query={"group_by": group_by, "districts": districts, "scope": scope,
                        "usage_zone_type": usage_zone_type, "min_year_built": min_year_built,
                        "max_year_built": max_year_built, "top_n": top_n, **sc},
                 notes=[cooling.note(sc), "Die Monatsverteilung ist eine pauschale Annahme (Mai–September), "
                        "für alle Nutzungsarten gleich."])


def density_grid(db: Session, bbox: str | None, polygon: dict | None = None, *, metric: str = "cooling_demand",
                 cell_m: int | None = None, top_n: int | None = None, usage_zone_type: str | None = None,
                 districts: list[str] | None = None, scope: str | None = None,
                 building_function: str | None = None, process_heat: bool | None = None,
                 climate: str | None = None, year: int | None = None, measures: list[str] | None = None,
                 setpoint: float | None = None, **extra) -> dict:
    cell = max(100, min(int(cell_m or 250), 1000))
    top = max(1, min(int(top_n or 400), 1500))
    params: dict = {"cell": cell, "top": top}
    sc = cooling.scenario(climate, year, measures, setpoint)
    cooling.bind(db, params, sc)
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False)
    where_filter = _filter_sql(params, usage_zone_type, None, None, db, building_function, process_heat, **extra)
    per_ha = 10000.0 / (cell * cell)
    cool = f"SUM({cooling.ANNUAL}) / 1000 * {per_ha}"
    heat = f"SUM(COALESCE(e.cons_c2r2, 0)) / 1000 * {per_ha}"
    expr, label, unit, sources = {
        "cooling_demand": (cool, "Kältedichte", "MWh/(ha·a)", ["cooling_mockup"]),
        "cooling_capacity": (f"SUM({cooling.CAPACITY}) * {per_ha}", "Dichte der benötigten Kälteleistung", "kW/ha",
                             ["cooling_mockup"]),
        "heat_cooling_overlap": (f"LEAST({cool}, {heat})", "Überlagerung Wärme und Kälte (kleinerer Wert)", "MWh/(ha·a)",
                                 ["cooling_mockup", "energymap_heating"]),
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
                      "usage_zone_type": usage_zone_type, **sc},
               notes=[f"Rasterzellen {cell} m × {cell} m, gezeigt werden die dichtesten {len(out)}.", cooling.note(sc)])
    fr["geo"] = True
    return {"layer": "cells", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr,
            "summary_for_llm": {"cells": len(out), "highest": out[0] if out else None, "unit": unit, "metric": label}}


def building_scenario_demand(db: Session, bbox: str | None, polygon: dict | None = None, *,
                             compare_measures: list[str] | None = None, climate: str | None = None,
                             year: int | None = None, setpoint: float | None = None, top_n: int | None = None,
                             districts: list[str] | None = None, scope: str | None = None, **_) -> dict:
    compare = [m for m in (compare_measures or []) if m in cooling.MEASURES]
    std = cooling.scenario(climate, year, None, setpoint)
    # without = the scenario's measures minus the asked ones; with = plus the asked ones
    sc_without = {**std, "measures": [m for m in std["measures"] if m not in compare]}
    sc_with = {**std, "measures": sorted(set(sc_without["measures"]) | set(compare))}
    top = max(1, min(int(top_n or 10), 500))
    params: dict = {"top": top}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False)
    cooling.bind(db, params, sc_without)
    # measures don't act on data centres and cold stores (process cooling): rank only buildings where they can act
    rows = db.execute(text(f"""
        SELECT b.uuid, b.namlag, b.hnr, b.bezgfk, cb.age_class, cb.ngf_m2,
               {cooling.ANNUAL} AS v_std, {cooling.ANNUAL_MIN} AS v_min, {cooling.ANNUAL_MAX} AS v_max,
               ST_AsGeoJSON(ST_Transform(b.geom, 4326), 6) AS geom
        {_BASE} {join}
        WHERE cb.measures_apply {where_area}
        ORDER BY v_std DESC NULLS LAST LIMIT :top"""), params).all()
    p2 = cooling.bind(db, {"uuids": [r.uuid for r in rows]}, sc_with)
    with_m = {r.uuid: r for r in db.execute(text(f"""
        SELECT cb.uuid, {cooling.ANNUAL} AS v_std, {cooling.ANNUAL_MIN} AS v_min, {cooling.ANNUAL_MAX} AS v_max
        FROM core_bldg.cooling_base_building cb WHERE cb.uuid = ANY(:uuids)"""), p2).all()}

    def mwh(v):
        return round(float(v) / 1000, 1) if v is not None else None

    out, features = [], []
    for k, r in enumerate(rows):
        w = with_m.get(r.uuid)
        address = r.namlag if r.namlag and r.hnr and r.namlag.endswith(r.hnr) else \
            " ".join(x for x in (r.namlag, r.hnr) if x) or None
        base = float(r.v_std or 0)
        saved = base - float(w.v_std or 0) if w else None
        out.append({"rank": k + 1, "bldg_uuid": r.uuid, "address": address, "building_function": r.bezgfk,
                    "age_class": r.age_class, "ngf_m2": round(float(r.ngf_m2 or 0)),
                    "demand_mwh_a": mwh(r.v_std), "demand_min_mwh_a": mwh(r.v_min), "demand_max_mwh_a": mwh(r.v_max),
                    "with_measures_mwh_a": mwh(w.v_std) if w else None,
                    "with_measures_min_mwh_a": mwh(w.v_min) if w else None,
                    "with_measures_max_mwh_a": mwh(w.v_max) if w else None,
                    "saving_mwh_a": mwh(saved), "saving_pct": round(100 * saved / base) if base and saved is not None else None})
        features.append({"type": "Feature", "geometry": json.loads(r.geom),
                         "properties": {"bldg_uuid": r.uuid, "rank": k + 1, "value": mwh(r.v_std)}})

    labels = db.execute(text("SELECT label, cool_pct_min, cool_pct_std, cool_pct_max FROM core_bldg.demand_measure "
                             "WHERE measure = ANY(:m)"), {"m": compare}).all()
    fields = [field("rank", "ordinal", "Rang"), field("bldg_uuid", "id", "Gebäude-ID"),
              field("address", "nominal", "Adresse"), field("building_function", "nominal", "Gebäudefunktion (ALKIS)"),
              field("age_class", "ordinal", "Baualtersklasse"), field("ngf_m2", "quantitative", "beheizte Fläche", "m²"),
              field("demand_mwh_a", "quantitative", "Kältebedarf ohne die Maßnahmen", "MWh/a"),
              field("demand_min_mwh_a", "quantitative", "min", "MWh/a"),
              field("demand_max_mwh_a", "quantitative", "max", "MWh/a"),
              field("with_measures_mwh_a", "quantitative", "mit Maßnahmen", "MWh/a"),
              field("with_measures_min_mwh_a", "quantitative", "mit Maßnahmen min", "MWh/a"),
              field("with_measures_max_mwh_a", "quantitative", "mit Maßnahmen max", "MWh/a"),
              field("saving_mwh_a", "quantitative", "Einsparung", "MWh/a"),
              field("saving_pct", "quantitative", "Einsparung", "%")]
    notes = [f"Ohne die Maßnahmen: {cooling.label(sc_without)}. Mit: {cooling.label(sc_with)}. Annahmen, keine Simulation."]
    if labels:
        notes.append("Maßnahmen: " + "; ".join(f"{l.label} {l.cool_pct_std:+.0f} % ({l.cool_pct_min:+.0f} bis "
                                                   f"{l.cool_pct_max:+.0f} %)" for l in labels)
                     + ". Wirkungen multiplizieren sich.")
    notes.append("Rechenzentren und Kühlhäuser (Prozesskälte) sind nicht in der Rangfolge, weil die Maßnahmen dort "
                 "nicht wirken.")
    fr = frame("features", fields, out, ["cooling_mockup", "alkis_function"], ids=[r["bldg_uuid"] for r in out],
               total=len(out), query={"compare_measures": compare, **std, "top_n": top, "districts": districts,
                                      "scope": scope}, notes=notes)
    return {"layer": "buildings", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr,
            "summary_for_llm": {"without_measures": cooling.label(sc_without), "with_measures": cooling.label(sc_with),
                                "unit": "MWh/a", "buildings": out[:10], "notes": notes}}


LINE_DENSITY_LABEL = {
    "standard": "Standard (2030, mittlerer Klimawandel, außenliegende Fensterverschattung)",
    "2025": "2025", "2045_gering": "2045, geringer Klimawandel", "2045_mittel": "2045, mittlerer Klimawandel",
    "2045_stark": "2045, starker Klimawandel",
    "2045_stark_massnahmen": "2045, starker Klimawandel, alle Maßnahmen, Sollwert 26 °C",
}


def line_density(db: Session, bbox: str | None, polygon: dict | None = None, *, carrier: str = "cooling",
                 scenario: str | None = None, group_by: str | None = None, basis: str | None = None,
                 min_length_m: float | None = None, min_buildings: int | None = None, top_n: int | None = None, districts: list[str] | None = None,
                 scope: str | None = None, **_) -> dict:
    carrier = "heat" if carrier == "heat" else "cooling"
    scenario = scenario if scenario in LINE_DENSITY_LABEL else "standard"
    trasse = basis != "street"
    by_street = group_by == "street"
    top = max(1, min(int(top_n or 300), 2000))
    params: dict = {"scenario": scenario, "top": top, "min_len": float(min_length_m or 0),
                    "min_n": int(min_buildings or 0)}
    where = ["l.scenario = :scenario"]
    if districts:
        ors = []
        for k, name in enumerate(districts):
            params[f"district_{k}"] = f"%{name}%"
            ors.append(f"l.district ILIKE :district_{k}")
        where.append(f"({' OR '.join(ors)})")
    elif polygon:
        where.append(f"ST_Intersects(l.geom, {_build_search_geom_expr(polygon, None, params)})")
    elif scope != "berlin" and bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
            where.append("l.geom && ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)")
            params.update({"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat})
        except ValueError:
            pass
    mwh = "l.heat_mwh_a" if carrier == "heat" else "l.cooling_mwh_a"
    length = "(l.street_m + l.connection_m)" if trasse else "l.street_m"
    key = "l.street_name, l.district" if by_street else "l.segment_id, l.street_name, l.district"
    rows = db.execute(text(f"""
        WITH g AS (
            SELECT {key}, SUM({mwh}) AS mwh, SUM({length}) AS len, SUM(l.street_m) AS street_m,
                   SUM(l.n_buildings) AS n, array_agg(l.segment_id) AS seg_ids,
                   ST_LineMerge(ST_Collect(l.geom)) AS geom
            FROM core_base.v_line_density l
            WHERE {' AND '.join(where)}
            GROUP BY {key}
            HAVING SUM(l.street_m) >= :min_len AND SUM(l.n_buildings) >= :min_n AND SUM({mwh}) > 0
        ), ranked AS (
            SELECT *, mwh * 1000 / NULLIF(len, 0) AS value, COUNT(*) OVER () AS total
            FROM g
        )
        SELECT street_name, district, value, mwh, street_m, n, total, seg_ids,
               ST_AsGeoJSON(ST_Transform(ST_Simplify(geom, 1), 4326), 6) AS geom,
               ST_X(ST_Transform(ST_PointOnSurface(geom), 4326)) AS lon,
               ST_Y(ST_Transform(ST_PointOnSurface(geom), 4326)) AS lat
        FROM ranked ORDER BY value DESC NULLS LAST LIMIT :top
    """), params).all()
    median = db.execute(text(f"""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY {mwh} * 1000 / NULLIF({length}, 0)) AS m,
               SUM({mwh}) * 1000 / NULLIF(SUM({length}), 0) AS mean
        FROM core_base.v_line_density l WHERE {' AND '.join(where)} AND l.street_m >= :min_len"""), params).one()
    label = ("Wärmeliniendichte" if carrier == "heat" else "Kälteliniendichte")
    unit = "kWh/(m·a)"
    features, out, seg_rank = [], [], {}
    for k, r in enumerate(rows):
        v = round(float(r.value))
        props = {"rank": k + 1, "value": v, "carrier": carrier, "street_name": r.street_name,
                 "district": r.district, "building_count": int(r.n)}
        features.append({"type": "Feature", "geometry": json.loads(r.geom), "properties": props})
        for sid in r.seg_ids:
            seg_rank[sid] = (k + 1, v)
        out.append({"rank": k + 1, "street_name": r.street_name, "district": r.district, "value": v,
                    "demand_mwh_a": round(float(r.mwh)), "length_m": round(float(r.street_m)),
                    "building_count": int(r.n), "lon": round(r.lon, 5), "lat": round(r.lat, 5)})
    # house connection lines of the shown sections: a network only pays off with the buildings connected
    n_conn = 0
    if seg_rank:
        # house connections are drawn thin and neutral, not classed: a single building's demand over a few metres
        # of pipe is no line density of a network section (computed that way it would mostly exceed the main lines)
        conns = db.execute(text("""
            SELECT c.segment_id, c.length_m, ST_AsGeoJSON(ST_Transform(c.geom, 4326), 6) AS geom
            FROM core_bldg.network_connection c WHERE c.segment_id = ANY(:ids) LIMIT 20000"""),
                           {"ids": list(seg_rank)}).all()
        for c in conns:
            rank, _ = seg_rank[c.segment_id]
            features.append({"type": "Feature", "geometry": json.loads(c.geom),
                             "properties": {"kind": "anschluss", "rank": rank, "carrier": carrier,
                                            "length_m": float(c.length_m)}})
        n_conn = len(conns)
    total = int(rows[0].total) if rows else 0
    demand_label = "Wärmebedarf (KWW)" if carrier == "heat" else "Kältebedarf"
    notes = [f"Szenario {LINE_DENSITY_LABEL[scenario]}. Liniendichte = Jahresbedarf der angeschlossenen beheizten "
             f"Gebäude je Meter {'Haupt- und Anschlussleitung' if trasse else 'Straßenabschnitt'} auf der "
             "potenziellen Netzstruktur (kein bestehendes Netz)."]
    if median.m is not None:
        notes.append(f"Median im Gebiet {round(median.m)} kWh/(m·a), Gesamtmittel {round(median.mean)} kWh/(m·a).")
    notes.append("Je höher die Liniendichte, desto günstiger für ein Netz; eine Wirtschaftlichkeitsprüfung ersetzt "
                 "das nicht." + (" Kältewerte sind Annahmen (Mockup), keine Simulation." if carrier == "cooling"
                                 else " Wärme nach KWW-Kennwerten (Heizung + Warmwasser)."))
    sources = ["street_network", "kww_heat" if carrier == "heat" else "cooling_mockup"]
    fr = frame("features",
               [field("rank", "ordinal", "Rang"), field("street_name", "nominal", "Straße"),
                field("district", "nominal", "Bezirk"), field("value", "quantitative", label, unit),
                field("demand_mwh_a", "quantitative", demand_label, "MWh/a"),
                field("length_m", "quantitative", "Abschnittslänge", "m"),
                field("building_count", "quantitative", "Anzahl Gebäude", "Gebäude"),
                field("lon", "ordinal", "Länge"), field("lat", "ordinal", "Breite")],
               out, sources, total=total,
               query={"carrier": carrier, "scenario": scenario, "group_by": group_by or "segment",
                      "basis": "trasse" if trasse else "street", "min_length_m": min_length_m, "min_buildings": min_buildings, "districts": districts, "scope": scope},
               notes=notes)
    fr["geo"] = True
    return {"layer": f"{carrier}_lines", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr,
            "summary_for_llm": {"shown": len(out), "total_sections": total, "unit": unit, "metric": label,
                                "scenario": LINE_DENSITY_LABEL[scenario], "top": out[:10],
                                "median_in_area": round(median.m) if median.m is not None else None,
                                "connection_lines_shown": n_conn,
                                "notes": notes}}


CHILLER_GROUPS = {"cooling_tower": "c.class LIKE 'CT_%'", "air_cooled": "c.class LIKE 'ACC_%'",
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
    sql = f'FROM core_bldg."Chillers" c {join} {where_sql}'
    metric = field("chiller_count", "quantitative", "Gebäude mit Kühlanlage" if by_building else "Erkannte Kühlanlagen",
                   "Gebäude" if by_building else "Anlagen")
    query = {"chiller_group": chiller_group, "count": count, "group_by": group_by, "districts": districts, "scope": scope}
    notes = ["Aus Luftbildern erkannt.", CHILLER_CAPACITY_NOTE]
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
    if name == "building_scenario_demand":
        return building_scenario_demand(db, bbox, polygon, **tool_input)
    if name == "line_density":
        return line_density(db, bbox, polygon, **tool_input)
    if name == "aggregate_chillers":
        return aggregate_chillers(db, bbox, polygon, **tool_input)
    if name == "report_missing_data":
        return report_missing_data(db, bbox, polygon, **tool_input)
    return None
