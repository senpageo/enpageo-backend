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
                    PROCESS_HEAT_PROP, SELECTION_PROPS, _build_search_geom_expr, building_function_condition,
                    process_heat_condition, selection_condition, usage_condition)

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
    "planning_area": ("b.mostly_in_pca::text", field("planning_area", "nominal", "Planungsraum (Nr.)")),
}
# groups that can be drawn as areas on the map: key -> SQL returning (key, geom 25833)
GROUP_GEOMS = {
    "district": "SELECT name AS k, geom FROM emc.district WHERE name = ANY(:keys)",
    "planning_area": "SELECT pca_id::text AS k, geom FROM emc.pca WHERE pca_id::text = ANY(:keys)",
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
    "electricity_demand_cooling": (f"SUM({cooling.ELEC})",
                                   field("electricity_demand_cooling", "quantitative",
                                         "Kältestrom bei Deckung des Bedarfs", "kWh/a"), ["cooling_mockup"]),
    "electric_power_cooling": (f"SUM({cooling.POWER})",
                               field("electric_power_cooling", "quantitative", "elektrische Leistung der Kälteerzeugung",
                                     "kW"), ["cooling_mockup"]),
    "waste_heat_demand": (f"SUM({cooling.WASTE})",
                          field("waste_heat_demand", "quantitative", "Abwärme der Kälteerzeugung (aus Bedarf)", "kWh/a"),
                          ["cooling_mockup"]),
    "waste_heat_actual": ("SUM(cact.cooling_kwh_mean + cact.electricity_kwh_mean)",
                          field("waste_heat_actual", "quantitative", "Abwärme erkannter Kälteanlagen", "kWh/a"),
                          ["chillers", "chiller_estimate"]),
    "waste_heat_power_actual": ("SUM(cact.capacity_kw_mean * (1 + cact.electricity_kwh_mean / NULLIF(cact.cooling_kwh_mean, 0)))",
                                field("waste_heat_power_actual", "quantitative", "Abwärmeleistung erkannter Kälteanlagen",
                                      "kW"), ["chillers", "chiller_estimate"]),
    "pet_14h": ("AVG(bcl.pet14h)", field("pet_14h", "quantitative", "Wärmebelastung am Tag (PET 14 Uhr, Mittel der Gebäude)", "°C"),
                ["climate_model"]),
    "t_air_04h": ("AVG(bcl.t2m04h)", field("t_air_04h", "quantitative", "Lufttemperatur nachts (4 Uhr, Mittel der Gebäude)", "°C"),
                  ["climate_model"]),
    "u_mean": ("AVG(env.u_mean)", field("u_mean", "quantitative", "mittlerer U-Wert (typisch, Mittel der Gebäude)", "W/(m²K)"),
               ["envelope_nais"]),
    "heated_area": ('SUM(pb."Heated area")', field("heated_area", "quantitative", "beheizte Fläche", "m²"),
                    ["building_params"]),
    "building_count": ("COUNT(*)", field("building_count", "quantitative", "Anzahl Gebäude", "Gebäude"),
                       ["footprints"]),
}

# Metrics from detected chillers only count buildings that have such a unit.
ACTUAL_METRICS = {"cooling_actual", "electricity_cooling", "chiller_capacity", "waste_heat_actual",
                  "waste_heat_power_actual"}
# metrics computed from the cooling demand mock-up, i.e. depending on the scenario
SCENARIO_METRICS = {"cooling_demand", "cooling_specific", "cooling_capacity", "electricity_demand_cooling",
                    "electric_power_cooling", "waste_heat_demand"}
# min-max bands: of the chiller estimates, and of the cooling demand assumptions
METRIC_BANDS = {
    "cooling_demand": (f"SUM({cooling.ANNUAL_MIN})", f"SUM({cooling.ANNUAL_MAX})"),
    "electricity_demand_cooling": (f"SUM({cooling.ELEC_MIN})", f"SUM({cooling.ELEC_MAX})"),
    "waste_heat_actual": ("SUM(cact.cooling_kwh_min + cact.electricity_kwh_min)",
                          "SUM(cact.cooling_kwh_max + cact.electricity_kwh_max)"),
    "cooling_actual": ("SUM(cact.cooling_kwh_min)", "SUM(cact.cooling_kwh_max)"),
    "electricity_cooling": ("SUM(cact.electricity_kwh_min)", "SUM(cact.electricity_kwh_max)"),
    "chiller_capacity": ("SUM(cact.capacity_kw_min)", "SUM(cact.capacity_kw_max)"),
}


# Cross-check of a demand-based metric with the estimate from detected chillers: (SQL, fields)
CHECKS = {
    "cooling_demand": (", SUM(cact.cooling_kwh_mean) AS act, COUNT(cact.uuid) AS n_act",
                       [field("cooling_actual", "quantitative", "Kälteverbrauch erkannter Anlagen (Abgleich)", "kWh/a"),
                        field("buildings_with_chiller", "quantitative", "Gebäude mit erkannter Anlage", "Gebäude"),
                        field("coverage_pct", "quantitative", "Anteil des Bedarfs, den erkannte Anlagen decken", "%")]),
    "cooling_capacity": (", SUM(cact.capacity_kw_mean) AS act, COUNT(cact.uuid) AS n_act",
                         [field("cooling_actual", "quantitative", "installierte Kälteleistung erkannter Anlagen (heute)", "kW"),
                          field("buildings_with_chiller", "quantitative", "Gebäude mit erkannter Anlage", "Gebäude"),
                          field("coverage_pct", "quantitative", "installiert heute im Verhältnis zu benötigt", "%")]),
}


def _check_values(r) -> dict:
    act = float(r.act) if r.act is not None else None
    return {"cooling_actual": round(act, 1) if act is not None else None, "buildings_with_chiller": r.n_act,
            "coverage_pct": round(100 * act / float(r.value), 1) if act is not None and r.value else None}


def _band_fields(metric: str, metric_field: dict) -> list[dict]:
    return [field(f"{metric}_min", "quantitative", f"{metric_field['label']} – min", metric_field.get("unit")),
            field(f"{metric}_max", "quantitative", f"{metric_field['label']} – max", metric_field.get("unit"))]


_FILTER_PROPS = {
    "usage_zone_type": {"type": "string", "description": "Restrict to one usage type (same values and group words as find_buildings)."},
    "building_function": BUILDING_FUNCTION_PROP,
    "process_heat": PROCESS_HEAT_PROP,
    **EXTRA_FILTER_PROPS,
    "postcodes": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these Berlin postcodes; overrides the map view."},
    **SELECTION_PROPS,
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
                                  "full-load hours), electricity_demand_cooling (electricity if the demand is met by "
                                  "chillers: demand / EER, 'Strombedarf für Kühlung'), electric_power_cooling (kW), "
                                  "waste_heat_demand (heat rejected by chillers meeting the demand), waste_heat_actual / "
                                  "waste_heat_power_actual (Abwärme of detected chillers, kWh / kW), u_mean (mean typical "
                                  "U-value of the buildings, NaiS-DB), pet_14h / t_air_04h (mean heat stress by day / night "
                                  "air temperature at the buildings, climate model 2022), heated_area, "
                                  "building_count"},
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

HEAT_STRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "time": {"type": "string", "enum": ["day", "night"],
                 "description": "day: heat stress at 2 pm (PET, gefühlte Temperatur), default; night: air temperature at "
                                "4 am (nächtliche Überwärmung, Tropennächte)"},
        "areas": {"type": "array", "items": {"type": "string", "enum": ["settlement", "streets", "green"]},
                  "description": "Which areas: settlement (built-up blocks, default), streets, green (parks, open space)"},
        "vulnerable_only": {"type": "boolean",
                            "description": "Only areas the city rates as very unfavourable day and night or climatically "
                                           "vulnerable (Planungshinweiskarte Stadtklima 2022)"},
        "top_n": {"type": "integer", "description": "Areas listed in the table, default 20"},
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
        **SELECTION_PROPS,
    },
    "required": [],
}

COOL_PLACES_SCHEMA = {
    "type": "object",
    "properties": {
        "max_distance_m": {"type": "number", "description": "Search radius around the address/place or map centre, default 2000 (10 km = 10000)"},
        "min_area_m2": {"type": "number", "description": "Minimum size of the green space, default 2000"},
        "top_n": {"type": "integer", "description": "Number of places, default 5"},
        **SELECTION_PROPS,
    },
    "required": [],
}

EXISTING_NETWORKS_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["all", "heat", "cooling"],
                 "description": "all (default), heat (district / local heating) or cooling (district cooling networks)"},
        "max_distance_m": {"type": "number",
                           "description": "Also report networks up to this distance from the area, default 500"},
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
        **SELECTION_PROPS,
    },
    "required": [],
}

BLOCK_ELECTRICITY_SCHEMA = {
    "type": "object",
    "properties": {
        "color_by": {"type": "string", "enum": ["electricity", "cooling_share", "cooling_demand_share"],
                     "description": "What the blocks are coloured by on the map: electricity (total consumption per "
                                    "block, default), cooling_share (share of the electricity of detected chillers), "
                                    "cooling_demand_share (share the chiller electricity would have if the whole "
                                    "cooling demand were met by chillers)"},
        "top_n": {"type": "integer", "description": "Blocks listed in the table, default 50"},
        **cooling.SCENARIO_PROPS,
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
        **SELECTION_PROPS,
    },
    "required": [],
}

COOLING_SURPLUS_SCHEMA = {
    "type": "object",
    "properties": {
        "radius_m": {"type": "number", "description": "Radius in which neighbours are checked, default 300"},
        "top_n": {"type": "integer", "description": "Buildings with the largest surplus, default 20"},
        **cooling.SCENARIO_PROPS,
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
        **SELECTION_PROPS,
    },
    "required": [],
}

COOLING_SCENARIOS_SCHEMA = {
    "type": "object",
    "properties": {
        "compare": {"type": "string", "enum": ["years", "climates", "measures", "setpoints"],
                    "description": "years: development over the years 2025-2050 ('wie entwickelt sich ... bis 2045', "
                                   "time series; with climate: only that scenario, without: all three); climates: low/"
                                   "medium/strong climate change in one year; measures: no measures vs. each measure and "
                                   "combinations ('welche Maßnahme(nkombination) reduziert am meisten', 'Potenzial "
                                   "Nachtkühlung'); setpoints: 22/24/26 °C."},
        "quantity": {"type": "string", "enum": ["demand", "electricity"],
                     "description": "demand (cooling demand, default) or electricity (Kältestrom if met by chillers)"},
        "years": {"type": "array", "items": {"type": "integer"},
                  "description": "With compare=years: the years, default 2025, 2030, ..., 2050"},
        **cooling.SCENARIO_PROPS,
        **_FILTER_PROPS,
    },
    "required": ["compare"],
}

COLOR_METRICS = {
    # metric -> (SQL value per building, label, unit, ramp, sources)
    "cooling_demand": (cooling.ANNUAL, "Kältebedarf", "kWh/a", "cool", ["cooling_mockup"]),
    "cooling_specific": (f"{cooling.ANNUAL} / NULLIF({cooling.AREA}, 0)", "spez. Kältebedarf", "kWh/(m²·a)", "cool",
                         ["cooling_mockup"]),
    "cooling_capacity": (cooling.CAPACITY, "benötigte Kälteleistung", "kW", "cool", ["cooling_mockup"]),
    "cooling_actual": ("cact.cooling_kwh_mean", "Kälteverbrauch (aus installierter Leistung)", "kWh/a", "cool",
                       ["chillers", "chiller_estimate"]),
    "electricity_demand_cooling": (cooling.ELEC, "Kältestrom bei Deckung des Bedarfs", "kWh/a", "cool", ["cooling_mockup"]),
    "heating_specific": ("e.cons_c2r2 / NULLIF(e.nrf_area, 0)", "spez. Heizwärmebedarf", "kWh/(m²·a)", "warm",
                         ["energymap_heating"]),
    "kww_heat_demand": ("kh.heat_dhw_kwh_a", "Wärmebedarf KWW (Heizung + Warmwasser)", "kWh/a", "warm", ["kww_heat"]),
    "pv_potential_kwp": ("pvp.potential_kwp", "PV-Potenzial Dach", "kWp", "warm", ["pv_potential"]),
    "u_mean": ("env.u_mean", "mittlerer U-Wert (typisch)", "W/(m²K)", "warm", ["envelope_nais"]),
    "u_wall": ("env.u_wall", "U-Wert Wand (typisch)", "W/(m²K)", "warm", ["envelope_nais"]),
    "u_window": ("env.u_window", "U-Wert Fenster (typisch)", "W/(m²K)", "warm", ["envelope_nais"]),
    "pet_14h": ("bcl.pet14h", "Wärmebelastung am Tag (PET 14 Uhr)", "°C", "warm", ["climate_model"]),
    "t_air_04h": ("bcl.t2m04h", "Lufttemperatur nachts (4 Uhr)", "°C", "warm", ["climate_model"]),
}

COLOR_BUILDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": list(COLOR_METRICS),
                   "description": "Value the buildings are coloured by ('färbe die Gebäude nach ...', 'wie ist ... "
                                  "verteilt', 'zeige das PV-Potenzial im Gebiet')."},
        **cooling.SCENARIO_PROPS,
        **_FILTER_PROPS,
    },
    "required": ["metric"],
}

BUILDING_SCENARIO_SCHEMA = {
    "type": "object",
    "properties": {
        "compare_measures": {"type": "array", "items": {"type": "string", "enum": cooling.MEASURES},
                             "description": "Measures whose effect is asked ('wie viel weniger, wenn ...'): "
                                            "verschattung_aussen (external window shading), lueftung_nutzer (ventilation "
                                            "by users), nachtlueftung_auto (automatic night ventilation), fenstertausch "
                                            "(new windows, solar control glass), sanierung (envelope retrofit)."},
        **{k: v for k, v in cooling.SCENARIO_PROPS.items() if k != "measures"},
        "top_n": {"type": "integer", "description": "Number of buildings with the highest cooling demand, default 10, max 500"},
        **SELECTION_PROPS,
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
        **cooling.SCENARIO_PROPS,
        "min_buildings": {"type": "integer",
                          "description": "Leave out sections/streets with fewer connected buildings. Use 5 for "
                                         "'wo lohnt sich ein Netz', so a single large building is not called a network."},
        "top_n": {"type": "integer", "description": "Keep the n highest, default 300, max 2000"},
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
    },
    "required": ["carrier"],
}

STREET_TREES_SCHEMA = {
    "type": "object",
    "properties": {
        "street": {"type": "string", "description": "Street name when no section is drawn on the map (e.g. 'Sonnenallee')"},
        "spacing_m": {"type": "number", "description": "Distance between new trees in m, default 15 ('alle 15 m einen Baum')"},
        **{k: v for k, v in cooling.SCENARIO_PROPS.items() if k != "measures"},
        "districts": _FILTER_PROPS["districts"],
        **SELECTION_PROPS,
    },
    "required": [],
}

INDOOR_OVERHEATING_SCHEMA = {
    "type": "object",
    "properties": {
        "threshold_c": {"type": "integer", "description": "Indoor temperature limit in °C, 26-32, default 30"},
        "min_days": {"type": "integer", "description": "More days above the limit than this = cooling unavoidable, default 10"},
        "compare_measures": {"type": "boolean",
                             "description": "true for one building or site: table of measure combinations and whether "
                                            "the building stays below the limit without cooling ('natürliche "
                                            "Klimatisierung ausreichend', 'welche Maßnahmen reichen')"},
        **cooling.SCENARIO_PROPS,
        "top_n": {"type": "integer", "description": "Buildings listed, default 20"},
        **_FILTER_PROPS,
        **SELECTION_PROPS,
    },
    "required": [],
}

RETROFIT_PATH_SCHEMA = {
    "type": "object",
    "properties": {
        "interval_years": {"type": "integer", "description": "One retrofit step every n years, default 4"},
        "per_step": {"type": "integer", "description": "Buildings retrofitted per step, default 1"},
        "start_year": {"type": "integer", "description": "First retrofit, default 2026"},
        "end_year": {"type": "integer", "description": "End of the path, default 2045"},
        "order": {"type": "string", "enum": ["highest_demand", "oldest"],
                  "description": "Which buildings first: highest cooling demand (default) or oldest"},
        "owner": {**EXTRA_FILTER_PROPS["owner"],
                  "description": "Which buildings are retrofitted; default land_berlin ('öffentliches Gebäude')"},
        **cooling.SCENARIO_PROPS,
        "usage_zone_type": _FILTER_PROPS["usage_zone_type"],
        "building_function": _FILTER_PROPS["building_function"],
        "districts": _FILTER_PROPS["districts"],
        "scope": _FILTER_PROPS["scope"],
        **SELECTION_PROPS,
    },
    "required": [],
}

TOOLS = [
    {
        "name": "heat_stress_map",
        "description": (
            "Outdoor heat stress from the Berlin climate model 2022 (current state, hot summer day): blocks, streets "
            "or green spaces coloured by heat stress at 2 pm (PET) or night air temperature at 4 am, with the hottest "
            "areas listed; optionally only areas the city rates as very unfavourable or vulnerable. Use for 'wo ist "
            "die Wärmebelastung am höchsten', 'Hitze-Hotspots', 'Tropennächte', 'Schwerpunkte der Wärmebelastung'. "
            "For buildings with high heat stress use find_buildings (min_pet_14h, sort_by pet_14h, has_chiller=false)."
        ),
        "input_schema": HEAT_STRESS_SCHEMA,
    },
    {
        "name": "cool_places",
        "description": (
            "The coolest green and open spaces near an address, a place or the map centre on a hot day (lowest PET "
            "at 2 pm, climate model 2022), with distance. Use for 'wo gibt es kühlere Orte während einer Hitzewelle'."
        ),
        "input_schema": COOL_PLACES_SCHEMA,
    },
    {
        "name": "existing_networks",
        "description": (
            "Existing heating and cooling networks near an area, address or site: district cooling networks (operator, "
            "capacity, annual energy, length, supply/return temperature, source), local heating networks per block "
            "(operator) and the areas supplied with district or local heating in 2022 (200 m grid), each with the "
            "distance. Use for 'gibt es ein Kältenetz / Fernwärme in der Nähe', 'wo liegen Wärmenetze'. Network "
            "routes (pipes) are not public."
        ),
        "input_schema": EXISTING_NETWORKS_SCHEMA,
    },
    {
        "name": "block_electricity",
        "description": (
            "Electricity consumption per statistical block (Berlin energy atlas, 2021) in an area, next to the "
            "electricity of detected chillers and the chiller electricity if the whole cooling demand were met by "
            "chillers, with shares; blocks coloured on the map. Use for 'Strombedarf/Stromverbrauch im Gebiet', 'wie "
            "setzt sich der Strombedarf zusammen', 'Anteil Kühlung am Strom'."
        ),
        "input_schema": BLOCK_ELECTRICITY_SCHEMA,
    },
    {
        "name": "cooling_surplus",
        "description": (
            "Buildings whose detected chillers have more capacity than their own cooling demand needs (Kälteüberschuss), "
            "with the neighbours within a radius that need cooling and have no detected chiller: count, required "
            "capacity, demand, distance. Use for 'welche Gebäude könnten Nachbarn mitversorgen', 'Kälteüberschuss'."
        ),
        "input_schema": COOLING_SURPLUS_SCHEMA,
    },
    {
        "name": "cooling_scenarios",
        "description": (
            "Cooling demand (or chiller electricity) of an area, a group of buildings or one building under several "
            "scenarios at once: development over the years 2025-2050 (line chart), climate scenarios compared, "
            "measures and their combinations compared (saving in %), or setpoints compared. Use for 'wie entwickelt "
            "sich', 'bis 2045', 'welche Maßnahme bringt am meisten', 'Potenzial Nachtlüftung' in an area, and for "
            "one building ('Charité in den nächsten 20 Jahren': place) or address."
        ),
        "input_schema": COOLING_SCENARIOS_SCHEMA,
    },
    {
        "name": "color_buildings",
        "description": (
            "Colour every building in the area by a value on the map (choropleth with legend): cooling demand, "
            "specific cooling demand, required capacity, consumption of detected chillers, chiller electricity, "
            "specific heating demand, KWW heat demand, rooftop PV potential. Use for 'färbe die Gebäude nach ...', "
            "'wie ist der Kühlbedarf im Gebiet verteilt', 'zeige das PV-Potenzial im Gebiet'."
        ),
        "input_schema": COLOR_BUILDINGS_SCHEMA,
    },
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
        "name": "street_trees_scenario",
        "description": (
            "Scenario: plant new street trees along a street section (drawn on the map as a line, or a street name or "
            "address) every n metres; shows the new trees on the map (purple, not saved) and the buildings whose "
            "facades they shade, with the number of trees, the cooling of the ambient air and the lower cooling "
            "demand. Use for 'wenn man an der Straße alle 15 m einen Baum pflanzt', 'Stadtgrün / Straßenbäume "
            "pflanzen'. The effects are made-up placeholders: always say so."
        ),
        "input_schema": STREET_TREES_SCHEMA,
    },
    {
        "name": "indoor_overheating",
        "description": (
            "Estimated indoor overheating without cooling: days per year above an indoor temperature (default 30 °C) "
            "and hours above 26 °C per building, for a year, climate scenario and measures; buildings above a number "
            "of days (default 10) need cooling. Use for 'wo wird eine Klimaanlage unvermeidlich', 'Innenraumtemperatur "
            "> 30 °C an mehr als 10 Tagen', 'reicht natürliche Klimatisierung' (compare_measures for one building), "
            "'welche Gebäude überhitzen'. 'Other measures exhausted' = all measures except sanierung. Placeholder "
            "estimate, not a simulation: always say so."
        ),
        "input_schema": INDOOR_OVERHEATING_SCHEMA,
    },
    {
        "name": "retrofit_path",
        "description": (
            "Cooling demand of an area year by year until 2045 with and without a retrofit path: every n years n "
            "buildings (default: one state-owned building every 4 years, highest cooling demand first) get an "
            "envelope retrofit; line chart plus the list of buildings and years. Use for 'wie ändert sich der "
            "Kältebedarf bis 2045, wenn ich alle 4 Jahre ein öffentliches Gebäude saniere'. Flat retrofit effect "
            "(placeholder): always say so."
        ),
        "input_schema": RETROFIT_PATH_SCHEMA,
    },
    {
        "name": "report_missing_data",
        "description": (
            "Call this when answering needs data the map does not have yet: simulated (not assumed) climate "
            "scenarios or measure effects, green roofs or facades, simulated indoor temperatures, future outdoor heat "
            "stress, electricity demand, PV, "
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
                 "kommen aus der Simulation im Projekt; Annahmen (Mockup) für Verschattung, Lüftung, Fenstertausch und Sanierung gibt es schon (building_scenario_demand, cooling_scenarios), Straßenbäume als Platzhalter (street_trees_scenario); Dach- und Fassadenbegrünung fehlen noch"),
    "indoor_temperature": ("Simulierte Innenraumtemperaturen und Überhitzungsstunden", "kommen aus der Simulation im Projekt; eine Platzhalter-Schätzung gibt es (indoor_overheating)"),
    "outdoor_heat": ("Wärmebelastung im Außenraum für künftige Jahre und Klimaszenarien",
                     "sind noch nicht im Datenbestand; den Ist-Zustand (Klimamodell 2022) gibt es (heat_stress_map, cool_places)"),
    "electricity": ("Strom nach Verbrauchern (Haushalt, Gewerbe) und Lastgänge",
                    "sind noch nicht im Datenbestand; der Stromverbrauch je Block (2021) und der Kältestrom sind da (block_electricity)"),
    "pv": ("PV-Erzeugung im Zeitverlauf und kleine PV-Anlagen je Gebäude",
           "Dachpotenzial (kWp) und Anlagen ab 30 kWp bzw. der öffentlichen Hand sind da (find_buildings), kleine Anlagen nur je PLZ"),
    "cooling_networks": ("Leitungsverläufe bestehender Wärme- und Kältenetze",
                         "sind nicht öffentlich; versorgte Gebiete, Nahwärmenetze und bekannte Kältenetze sind da (existing_networks)"),
    "chiller_capacity": ("gemessene Kälteleistungen je erkannter Anlage",
                         "es gibt die KIT-Schätzung je Anlage aus dem Luftbild (Metrik chiller_capacity, mit Spanne; find_chillers je Anlage)"),
    "other_city": ("Gebäudedaten außerhalb Berlins", "Stuttgart und Korbach folgen im Projekt, weitere Städte sind nicht vorgesehen"),
    "ownership": ("Eigentum außer dem Land Berlin (Bund, Private, Gesellschaften, Wohnungsbaugesellschaften) je Gebäude",
                  "landeseigene Grundstücke sind da (Filter owner), andere Eigentümergruppen gibt es nur als Anteil je Planungsraum"),
    "building_physics": ("Tatsächlicher Sanierungsstand und gemessene U-Werte je Gebäude",
                         "sind nicht im Datenbestand; typische U-Werte nach Gebäudetyp und Baualter (NaiS-DB) sind da"),
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
                building_function: str | None = None, process_heat: bool | None = None,
                address: str | None = None, place: str | None = None, **extra) -> str:
    cond = extra_conditions(params, **extra)
    if address or place:
        sel, _, error = selection_condition(db, params, address, place)
        if error:
            raise LookupError(error)
        cond.append(sel)
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
    check = metric in CHECKS
    band_sql += CHECKS[metric][0] if check else ""
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
        fields += CHECKS[metric][1]
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
             "process_heat": process_heat, **(sc if metric in SCENARIO_METRICS else {})}
    cooling_notes = ([cooling.note(sc)] + ([cooling.ACTUAL_NOTE] if metric == "cooling_demand" else [])
                     + (["Kältestrom = Kältebedarf / Leistungszahl der Kälteerzeugung je Nutzung (luftgekühlt 2,7, "
                         "wassergekühlt 3,9; Annahme), wenn der ganze Bedarf mit Kälteanlagen gedeckt wird."]
                        if metric in ("electricity_demand_cooling", "electric_power_cooling", "waste_heat_demand") else [])
                     + (["Benötigte Kälteleistung = Kältebedarf / Volllaststunden der Nutzung (Annahme)."]
                        if metric == "cooling_capacity" else []))
    if not group_by:
        notes = ([COOLING_ACTUAL_NOTE] if metric in ACTUAL_METRICS else
                 cooling_notes if metric in SCENARIO_METRICS else [])
        return _single_value(db, metric, agg_sql, metric_field, metric_sources, join, where_area, where_filter, params,
                             share_of_total, query, notes)
    keys = [GROUPS[g][0] for g in group_by]
    select_keys = ", ".join(f"{k} AS g{n}" for n, k in enumerate(keys))
    group_keys = ", ".join(f"g{n}" for n in range(len(keys)))
    band_sql = f", {band[0]} AS vmin, {band[1]} AS vmax" if band else ""
    check = metric in CHECKS
    band_sql += CHECKS[metric][0] if check else ""
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
              + (CHECKS[metric][1] if check else []))
    if metric != "building_count":
        fields.append(METRICS["building_count"][1])
    sources = ["building_params", *metric_sources] + (["districts"] if (districts or "district" in group_by) else [])
    notes = []
    if not out:
        notes.append("Keine Gebäude in diesem Gebiet im Datenbestand. Vorhanden ist derzeit nur Berlin.")
    elif metric in ACTUAL_METRICS:
        notes.append(COOLING_ACTUAL_NOTE)
    elif metric in SCENARIO_METRICS:
        notes += cooling_notes
    elif metric in ("pet_14h", "t_air_04h"):
        notes.append("Wärmebelastung: Klimamodell Berlin 2022 (Umweltatlas) für eine austauscharme sommerliche Strahlungswetterlage, Ist-Zustand; PET = gefühlte Temperatur um 14 Uhr, Lufttemperatur um 4 Uhr = nächtliche Überwärmung, je Block(teil)fläche.")
    elif metric == "u_mean":
        notes.append("U-Werte: typische Werte nach Gebäudetyp und Baualter aus der NaiS-DB (Wohnen TABULA-Typgebäude im Ist-Zustand, Nichtwohnen IWU-NWG-Mittelwerte mit Standardfehler); der tatsächliche Sanierungsstand je Gebäude ist nicht bekannt.")
    fr = frame("aggregate", fields, out, sources, hierarchy=group_by if len(group_by) > 1 else None,
               total=total_groups, query=query, notes=notes)
    if len(group_by) == 1 and group_by[0] in GROUP_GEOMS and out:
        values = {r[group_by[0]]: r[metric] for r in out if r[metric] is not None}
        geoms = db.execute(text(f"SELECT k, ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(geom, 5), 4326), 6) AS g "
                                f"FROM ({GROUP_GEOMS[group_by[0]]}) x"), {"keys": list(values)}).all()
        features = [{"type": "Feature", "geometry": json.loads(g.g), "properties": {group_by[0]: g.k, "value": values[g.k]}}
                    for g in geoms if g.k in values]
        if features:
            return {"layer": "cells", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr}
    return fr


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


def _scenario_groups(db: Session, bbox, polygon, quantity: str, districts, scope, postcodes, usage_zone_type,
                     min_year_built, max_year_built, building_function, process_heat, **extra):
    """Sums of the base demand per (measures_apply, setpoint_base_c) for the requested buildings, plus the consumption
    of detected chillers in them (cross-check)."""
    params: dict = {}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False, postcodes)
    where_filter = _filter_sql(params, usage_zone_type, min_year_built, max_year_built, db, building_function,
                               process_heat, **extra)
    div = {"min": "/ NULLIF(cb.eer_max, 0)", "std": "/ NULLIF(cb.eer_mean, 0)", "max": "/ NULLIF(cb.eer_min, 0)"} \
        if quantity == "electricity" else {"min": "", "std": "", "max": ""}
    groups = db.execute(text(f"""
        SELECT cb.measures_apply AS ma, cb.setpoint_base_c AS sb,
               SUM(cb.kwh_min {div['min']}) AS s_min, SUM(cb.kwh_std {div['std']}) AS s_std,
               SUM(cb.kwh_max {div['max']}) AS s_max, COUNT(*) AS n,
               SUM(cact.cooling_kwh_mean) AS act, COUNT(cact.uuid) AS n_act
        {_BASE} {join}
        WHERE cb.uuid IS NOT NULL {where_area} {where_filter}
        GROUP BY 1, 2"""), params).all()
    return groups


def cooling_scenarios(db: Session, bbox: str | None, polygon: dict | None = None, *, compare: str = "years",
                      quantity: str = "demand", years: list[int] | None = None, climate: str | None = None,
                      year: int | None = None, measures: list[str] | None = None, setpoint: float | None = None,
                      districts: list[str] | None = None, scope: str | None = None, postcodes: list[str] | None = None,
                      usage_zone_type: str | None = None, min_year_built: int | None = None,
                      max_year_built: int | None = None, building_function: str | None = None,
                      process_heat: bool | None = None, **extra) -> dict:
    quantity = "electricity" if quantity == "electricity" else "demand"
    base = cooling.scenario(climate, year, measures, setpoint)
    groups = _scenario_groups(db, bbox, polygon, quantity, districts, scope, postcodes, usage_zone_type, min_year_built,
                              max_year_built, building_function, process_heat, **extra)
    n = sum(g.n for g in groups)
    act = sum(float(g.act or 0) for g in groups)
    n_act = sum(g.n_act for g in groups)
    label = "Kältebedarf" if quantity == "demand" else "Kältestrom bei Deckung des Bedarfs"

    def values(sc):
        f = cooling.bind(db, {}, sc)
        return {b: round(cooling.total(groups, f, b) / 1000, 1) for b in ("min", "std", "max")}

    query = {"compare": compare, "quantity": quantity, **base, "districts": districts, "scope": scope,
             "usage_zone_type": usage_zone_type, "building_function": building_function, **extra}
    notes = []
    if compare == "years":
        yrs = sorted({max(2025, min(int(y), 2050)) for y in (years or range(2025, 2051, 5))})
        climates = [climate] if climate in cooling.CLIMATES else list(cooling.CLIMATES)
        rows = []
        for cl in climates:
            for y in yrs:
                v = values({**base, "climate": cl, "year": y})
                rows.append({"year": y, "year_label": str(y), "climate": cooling.CLIMATES[cl],
                             "value": v["std"], "value_min": v["min"], "value_max": v["max"]})
        fields = [field("year", "temporal", "Jahr")] + ([field("climate", "nominal", "Klimaszenario")] if len(climates) > 1 else []) \
            + [field("value", "quantitative", label, "MWh/a"), field("value_min", "quantitative", "min", "MWh/a"),
               field("value_max", "quantitative", "max", "MWh/a")]
        kind = "series"
        notes.append(f"Entwicklung {yrs[0]}–{yrs[-1]}; sonst wie {cooling.label(base).split(', ', 2)[2]}. Annahmen, keine Simulation.")
    else:
        if compare == "climates":
            scs = [(cooling.CLIMATES[c], {**base, "climate": c}) for c in cooling.CLIMATES]
            key = field("scenario", "ordinal", "Klimaszenario")
        elif compare == "setpoints":
            scs = [("Sollwert nach Gebäudefunktion", {**base, "setpoint": None})] + \
                  [(f"{sp} °C", {**base, "setpoint": float(sp)}) for sp in cooling.SETPOINTS]
            key = field("scenario", "ordinal", "Sollwert")
        else:  # measures
            from itertools import combinations
            # the measures named in the question, else all: each alone, pairs (up to three measures) and all together
            ms = [m for m in (measures or []) if m in cooling.MEASURES] or cooling.MEASURES
            combos = [()] + [(m,) for m in ms] + (list(combinations(ms, 2)) if len(ms) <= 3 else [])
            if len(ms) >= 3:
                combos.append(tuple(ms))
            scs = [(("alle Maßnahmen" if len(c) == len(cooling.MEASURES) else
                     " + ".join(cooling.MEASURE_LABEL[m] for m in c) or "ohne Maßnahmen"), {**base, "measures": list(c)})
                   for c in combos]
            key = field("scenario", "nominal", "Maßnahmen")
        rows = []
        for name, sc in scs:
            v = values(sc)
            rows.append({"scenario": name, "value": v["std"], "value_min": v["min"], "value_max": v["max"]})
        ref = rows[0]["value"]
        for r in rows:
            r["change_pct"] = round(100 * (r["value"] - ref) / ref, 1) if ref else None
        fields = [key, field("value", "quantitative", label, "MWh/a"), field("value_min", "quantitative", "min", "MWh/a"),
                  field("value_max", "quantitative", "max", "MWh/a"),
                  field("change_pct", "quantitative", f"Änderung gegenüber {rows[0]['scenario']}", "%")]
        kind = "aggregate"
        notes.append(f"Sonst wie {cooling.label(base)}. Annahmen, keine Simulation.")
    if quantity == "electricity":
        notes.append("Kältestrom = Kältebedarf / Leistungszahl der Kälteerzeugung je Nutzung (luftgekühlt 2,7, "
                     "wassergekühlt 3,9; Annahme), wenn der ganze Bedarf mit Kälteanlagen gedeckt wird.")
    if n_act:
        notes.append(f"Abgleich: Kälteverbrauch erkannter Anlagen heute {round(act / 1000):,} MWh/a an {n_act} Gebäuden."
                     .replace(",", "."))
    fr = frame(kind, fields, rows, ["cooling_mockup", "building_params"] + (["chillers", "chiller_estimate"] if n_act else []),
               total=len(rows), query=query, notes=notes)
    if n == 0:
        fr["notes"] = ["Keine beheizten Gebäude mit Kältebedarf in dieser Auswahl."]
    return fr


def color_buildings(db: Session, bbox: str | None, polygon: dict | None = None, *, metric: str = "cooling_demand",
                    climate: str | None = None, year: int | None = None, measures: list[str] | None = None,
                    setpoint: float | None = None, districts: list[str] | None = None, scope: str | None = None,
                    postcodes: list[str] | None = None, usage_zone_type: str | None = None,
                    min_year_built: int | None = None, max_year_built: int | None = None,
                    building_function: str | None = None, process_heat: bool | None = None, **extra) -> dict:
    if metric not in COLOR_METRICS:
        return {"error": f"unknown metric {metric}"}
    expr, label, unit, ramp, sources = COLOR_METRICS[metric]
    sc = cooling.scenario(climate, year, measures, setpoint)
    params: dict = {"cap": 40000}
    cooling.bind(db, params, sc)
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False, postcodes)
    where_filter = _filter_sql(params, usage_zone_type, min_year_built, max_year_built, db, building_function,
                               process_heat, **extra)
    rows = db.execute(text(f"""
        SELECT b.uuid, {expr} AS v, b.namlag, b.hnr, kg.gfk_name AS bf
        {_BASE} {join}
        WHERE {expr} IS NOT NULL AND {expr} > 0 {where_area} {where_filter}
        ORDER BY v DESC LIMIT :cap"""), params).all()
    features = [{"type": "Feature", "geometry": None, "properties": {"bldg_uuid": r.uuid, "value": round(float(r.v), 1)}}
                for r in rows]
    top = [{"rank": k + 1, "bldg_uuid": r.uuid,
            "address": r.namlag if r.namlag and r.hnr and r.namlag.endswith(r.hnr) else " ".join(x for x in (r.namlag, r.hnr) if x) or None,
            "building_function": r.bf, "value": round(float(r.v), 1)} for k, r in enumerate(rows[:50])]
    vals = sorted(float(r.v) for r in rows)
    q = (lambda p: round(vals[int(p * (len(vals) - 1))], 1)) if vals else (lambda p: None)
    notes = [f"{len(rows)} Gebäude eingefärbt" + (" (Obergrenze erreicht)" if len(rows) == params["cap"] else "")
             + f"; Median {q(0.5)}, 10 %: {q(0.1)}, 90 %: {q(0.9)} {unit}."]
    if metric.startswith("cooling") or metric == "electricity_demand_cooling":
        notes.append(cooling.note(sc) if metric != "cooling_actual" else cooling.ACTUAL_NOTE)
    if metric in ("pet_14h", "t_air_04h"):
        notes.append("Wärmebelastung: Klimamodell Berlin 2022 (Umweltatlas) für eine austauscharme sommerliche Strahlungswetterlage, Ist-Zustand; PET = gefühlte Temperatur um 14 Uhr, Lufttemperatur um 4 Uhr = nächtliche Überwärmung, je Block(teil)fläche.")
    if metric.startswith("u_"):
        notes.append("U-Werte: typische Werte nach Gebäudetyp und Baualter aus der NaiS-DB (Wohnen TABULA-Typgebäude im Ist-Zustand, Nichtwohnen IWU-NWG-Mittelwerte mit Standardfehler); der tatsächliche Sanierungsstand je Gebäude ist nicht bekannt.")
    fr = frame("features", [field("rank", "ordinal", "Rang"), field("bldg_uuid", "id", "Gebäude-ID"),
                            field("address", "nominal", "Adresse"), field("building_function", "nominal", "Gebäudefunktion (ALKIS)"),
                            field("value", "quantitative", label, unit)],
               top, ["footprints", *sources], total=len(rows),
               query={"metric": metric, **sc, "districts": districts, "scope": scope, **extra}, notes=notes)
    fr["geo"] = True
    return {"layer": f"building_values_{ramp}", "geojson": {"type": "FeatureCollection", "features": features},
            "frame": fr, "summary_for_llm": {"buildings": len(rows), "metric": label, "unit": unit, "median": q(0.5),
                                             "p10": q(0.1), "p90": q(0.9), "highest": top[:5], "notes": notes}}


def _search_area(db: Session, params: dict, bbox, polygon, districts, scope, address=None, place=None):
    """SQL expression (25833) of the area asked about, or None for all of Berlin, plus notes."""
    if address or place:
        cond, notes, error = selection_condition(db, params, address, place)
        if error:
            raise LookupError(error)
        return f"(SELECT ST_Union(b.geom) FROM emc.building b WHERE {cond})", notes
    if districts:
        ors = []
        for k, name in enumerate(districts):
            params[f"district_{k}"] = f"%{name}%"
            ors.append(f"d.name ILIKE :district_{k}")
        return f"(SELECT ST_Union(d.geom) FROM emc.district d WHERE {' OR '.join(ors)})", []
    if polygon:
        return _build_search_geom_expr(polygon, None, params), []
    if scope != "berlin" and bbox:
        try:
            min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
            params.update({"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat})
            return "ST_Transform(ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326), 25833)", []
        except ValueError:
            pass
    return None, []


KIND = {"settlement": "siedlung", "streets": "verkehr", "green": "gruen"}
KIND_LABEL = {"siedlung": "Siedlungsfläche", "verkehr": "Verkehrsfläche", "gruen": "Grün-/Freifläche"}


def heat_stress_map(db: Session, bbox: str | None, polygon: dict | None = None, *, time: str = "day",
                    areas: list[str] | None = None, vulnerable_only: bool | None = None, top_n: int | None = None,
                    districts: list[str] | None = None, scope: str | None = None, address: str | None = None,
                    place: str | None = None, **_) -> dict:
    col = "t2m04h" if time == "night" else "pet14h"
    kinds = [KIND[k] for k in (areas or ["settlement"]) if k in KIND] or ["siedlung"]
    params: dict = {"kinds": kinds}
    area, notes = _search_area(db, params, bbox, polygon, districts, scope, address, place)
    where = [f"c.kind = ANY(:kinds)", f"c.{col} IS NOT NULL"]
    if area:
        where.append(f"ST_Intersects(c.geom, ({area}))")
    if vulnerable_only:
        where.append("EXISTS (SELECT 1 FROM core_base.climate_vulnerable v WHERE ST_Intersects(v.geom, ST_PointOnSurface(c.geom)))")
    rows = db.execute(text(f"""
        SELECT c.kind, c.schl5, c.{col} AS v, c.rating, ST_Area(c.geom) AS a,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(c.geom, 2), 4326), 6) AS g,
               ST_X(ST_Transform(ST_PointOnSurface(c.geom), 4326)) AS lon, ST_Y(ST_Transform(ST_PointOnSurface(c.geom), 4326)) AS lat
        FROM core_base.climate_area c WHERE {' AND '.join(where)}
        ORDER BY c.{col} DESC LIMIT 6000"""), params).all()
    unit, label = "°C", ("Lufttemperatur nachts (4 Uhr)" if col == "t2m04h" else "Wärmebelastung am Tag (PET 14 Uhr)")
    features = [{"type": "Feature", "geometry": json.loads(r.g), "properties": {"value": round(float(r.v), 1)}} for r in rows]
    top = max(1, min(int(top_n or 20), 200))
    out = [{"rank": k + 1, "area_type": KIND_LABEL[r.kind], "block": r.schl5, "value": round(float(r.v), 1),
            "rating": r.rating, "area_m2": round(float(r.a)), "lon": round(r.lon, 5), "lat": round(r.lat, 5)}
           for k, r in enumerate(rows[:top])]
    # statistics over all matching areas (the map shows at most 6000)
    st = db.execute(text(f"""
        SELECT count(*) AS n, SUM(ST_Area(c.geom)) AS a, SUM(c.{col} * ST_Area(c.geom)) / NULLIF(SUM(ST_Area(c.geom)), 0) AS mean,
               SUM(ST_Area(c.geom)) FILTER (WHERE c.{col} > :t1) AS a1, SUM(ST_Area(c.geom)) FILTER (WHERE c.{col} > :t2) AS a2,
               max(c.{col}) AS vmax
        FROM core_base.climate_area c WHERE {' AND '.join(where)}"""),
                    {**params, "t1": 35 if col == "pet14h" else 19, "t2": 41 if col == "pet14h" else 20}).one()
    if st.n:
        p1, p2 = 100 * float(st.a1 or 0) / float(st.a), 100 * float(st.a2 or 0) / float(st.a)
        if col == "pet14h":
            notes.append(f"{st.n} Flächen, flächengewichtetes Mittel {float(st.mean):.1f} °C PET (höchster Wert "
                         f"{float(st.vmax):.1f} °C); starke Wärmebelastung (> 35 °C) auf {p1:.0f} % der Fläche, extreme "
                         f"(> 41 °C) auf {p2:.0f} %.")
        else:
            notes.append(f"{st.n} Flächen, flächengewichtetes Mittel {float(st.mean):.1f} °C um 4 Uhr (höchster Wert "
                         f"{float(st.vmax):.1f} °C); über 19 °C auf {p1:.0f} % der Fläche, über 20 °C auf {p2:.0f} %.")
        if st.n > len(rows):
            notes.append(f"Auf der Karte die {len(rows)} wärmsten Flächen.")
    notes.append("Wärmebelastung: Klimamodell Berlin 2022 (Umweltatlas) für eine austauscharme sommerliche Strahlungswetterlage, Ist-Zustand; PET = gefühlte Temperatur um 14 Uhr, Lufttemperatur um 4 Uhr = nächtliche Überwärmung, je Block(teil)fläche. Zukunftsszenarien der Wärmebelastung gibt es noch nicht.")
    fr = frame("features", [field("rank", "ordinal", "Rang"), field("area_type", "nominal", "Fläche"),
                            field("block", "nominal", "Block(teil)fläche"), field("value", "quantitative", label, unit),
                            field("rating", "nominal", "Bewertung"), field("area_m2", "quantitative", "Größe", "m²"),
                            field("lon", "ordinal", "Länge"), field("lat", "ordinal", "Breite")],
               out, ["climate_model"], total=len(rows),
               query={"time": time, "areas": areas, "vulnerable_only": vulnerable_only, "districts": districts,
                      "scope": scope, "address": address, "place": place}, notes=notes)
    fr["geo"] = True
    return {"layer": "cells_warm", "geojson": {"type": "FeatureCollection", "features": features, "legend_unit": unit},
            "frame": fr, "summary_for_llm": {"areas": len(rows), "unit": unit, "metric": label, "hottest": out[:5],
                                             "notes": notes}}


def cool_places(db: Session, bbox: str | None, polygon: dict | None = None, *, max_distance_m: float | None = None,
                min_area_m2: float | None = None, top_n: int | None = None, address: str | None = None,
                place: str | None = None, **_) -> dict:
    params: dict = {"dist": float(max_distance_m or 2000), "min_a": float(min_area_m2 or 2000),
                    "top": max(1, min(int(top_n or 5), 50))}
    notes: list[str] = []
    if address or place:
        ref, notes = _search_area(db, params, None, None, None, None, address, place)
        ref = f"ST_PointOnSurface({ref})"
    elif polygon:
        ref = f"ST_PointOnSurface({_build_search_geom_expr(polygon, None, params)})"
    elif bbox:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
        params.update({"lon": (min_lon + max_lon) / 2, "lat": (min_lat + max_lat) / 2})
        ref = "ST_Transform(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 25833)"
        notes.append("Ausgangspunkt: Mitte des Kartenausschnitts.")
    else:
        return {"error": "Ohne Adresse, Ort oder Kartenausschnitt fehlt der Ausgangspunkt"}
    rows = db.execute(text(f"""
        WITH r AS (SELECT {ref} AS p)
        SELECT c.schl5, c.pet14h, c.t2m04h, c.rating, ST_Area(c.geom) AS a, ST_Distance(c.geom, r.p) AS d,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(c.geom, 2), 4326), 6) AS g,
               ST_X(ST_Transform(ST_PointOnSurface(c.geom), 4326)) AS lon, ST_Y(ST_Transform(ST_PointOnSurface(c.geom), 4326)) AS lat
        FROM core_base.climate_area c, r
        WHERE c.kind = 'gruen' AND c.pet14h IS NOT NULL AND ST_DWithin(c.geom, r.p, :dist) AND ST_Area(c.geom) >= :min_a
        ORDER BY c.pet14h, ST_Distance(c.geom, r.p) LIMIT :top"""), params).all()
    out, features = [], []
    for k, r in enumerate(rows):
        out.append({"rank": k + 1, "block": r.schl5, "pet_14h": round(float(r.pet14h), 1),
                    "t_air_04h": round(float(r.t2m04h), 1) if r.t2m04h is not None else None, "rating": r.rating,
                    "area_m2": round(float(r.a)), "distance_m": round(float(r.d)), "lon": round(r.lon, 5),
                    "lat": round(r.lat, 5)})
        features.append({"type": "Feature", "geometry": json.loads(r.g), "properties": {"value": round(float(r.pet14h), 1)}})
    notes.append(f"Kühlste Grün- und Freiflächen ab {params['min_a']:.0f} m² im Umkreis von {params['dist']:.0f} m, "
                 "nach gefühlter Temperatur (PET) um 14 Uhr. Gewässer und Wasserflächen sind nicht enthalten; die "
                 "Erreichbarkeit (Zugang, Öffnungszeiten) ist nicht geprüft.")
    notes.append("Wärmebelastung: Klimamodell Berlin 2022 (Umweltatlas) für eine austauscharme sommerliche Strahlungswetterlage, Ist-Zustand; PET = gefühlte Temperatur um 14 Uhr, Lufttemperatur um 4 Uhr = nächtliche Überwärmung, je Block(teil)fläche.")
    fr = frame("features", [field("rank", "ordinal", "Rang"), field("block", "nominal", "Block(teil)fläche"),
                            field("pet_14h", "quantitative", "Wärmebelastung am Tag (PET 14 Uhr)", "°C"),
                            field("t_air_04h", "quantitative", "Lufttemperatur nachts (4 Uhr)", "°C"),
                            field("rating", "nominal", "Aufenthaltsqualität am Tag"),
                            field("area_m2", "quantitative", "Größe", "m²"), field("distance_m", "quantitative", "Entfernung", "m"),
                            field("lon", "ordinal", "Länge"), field("lat", "ordinal", "Breite")],
               out, ["climate_model"], total=len(out),
               query={"max_distance_m": params["dist"], "min_area_m2": params["min_a"], "address": address,
                      "place": place}, notes=notes)
    fr["geo"] = True
    return {"layer": "cells", "geojson": {"type": "FeatureCollection", "features": features, "legend_unit": "°C"},
            "frame": fr, "summary_for_llm": {"places": out, "notes": notes}}


def existing_networks(db: Session, bbox: str | None, polygon: dict | None = None, *, kind: str = "all",
                      max_distance_m: float | None = None, districts: list[str] | None = None,
                      scope: str | None = None, address: str | None = None, place: str | None = None, **_) -> dict:
    kind = kind if kind in ("heat", "cooling") else "all"
    params: dict = {"dist": float(max_distance_m or 500)}
    area, notes = _search_area(db, params, bbox, polygon, districts, scope, address, place)
    near = (lambda g: f"ST_DWithin({g}, a.g, :dist)") if area else (lambda g: "true")
    dist = (lambda g: f"round(ST_Distance({g}, a.g))") if area else (lambda g: "0")
    a_cte = f"WITH a AS (SELECT {area} AS g) " if area else "WITH a AS (SELECT NULL::geometry AS g) "
    features, rows = [], []
    if kind in ("all", "cooling"):
        for r in db.execute(text(a_cte + f"""
            SELECT n.*, {dist('n.area_geom')} AS d_area, {dist('n.plant_geom')} AS d_plant,
                   ST_AsGeoJSON(ST_Transform(n.area_geom, 4326), 6) AS ga, ST_AsGeoJSON(ST_Transform(n.plant_geom, 4326), 6) AS gp
            FROM core_base.cooling_network n, a WHERE {near('n.area_geom')} ORDER BY d_area"""), params).all():
            rows.append({"type": "Kältenetz", "name": r.name, "operator": r.operator, "distance_m": float(r.d_area),
                         "capacity_mw": float(r.capacity_mw) if r.capacity_mw else None,
                         "energy_mwh_a": float(r.energy_mwh_a) if r.energy_mwh_a else None,
                         "length_km": float(r.length_km) if r.length_km else None,
                         "temperatures": f"{r.t_supply_c:g}/{r.t_return_c:g} °C" if r.t_supply_c else None,
                         "detail": f"Kältezentrale {r.plant}; Kunden u. a. {', '.join(r.customers[:4])}. {r.remark}",
                         "source": r.source})
            features += [{"type": "Feature", "geometry": json.loads(r.ga), "properties": {"kind": "cooling", "name": r.name}},
                         {"type": "Feature", "geometry": json.loads(r.gp), "properties": {"kind": "cooling_plant", "name": r.plant}}]
    if kind in ("all", "heat"):
        local = db.execute(text(a_cte + f"""
            SELECT n.operator, count(*) AS n, min({dist('n.geom')}) AS d,
                   json_agg(ST_AsGeoJSON(ST_Transform(n.geom, 4326), 6)) AS gs
            FROM core_base.local_heat_network n, a WHERE {near('n.geom')} GROUP BY 1 ORDER BY d LIMIT 50"""), params).all()
        for r in local:
            rows.append({"type": "Nahwärmenetz", "name": f"{r.n} Teilblock" if r.n == 1 else f"{r.n} Teilblöcke",
                         "operator": r.operator, "distance_m": float(r.d), "capacity_mw": None, "energy_mwh_a": None,
                         "length_km": None, "temperatures": None, "detail": None,
                         "source": "Wärmeplan Berlin, Bestandsanalyse (gdi.berlin.de)"})
            features += [{"type": "Feature", "geometry": json.loads(g), "properties": {"kind": "local_heat", "name": r.operator}}
                         for g in r.gs[:500]]
        cells = db.execute(text(a_cte + f"""
            SELECT count(*) AS n, min({dist('c.geom')}) AS d,
                   (array_agg(ST_AsGeoJSON(ST_Transform(c.geom, 4326), 6)))[1:3000] AS gs
            FROM core_base.heat_supply_cell c, a WHERE {near('c.geom')}"""), params).one()
        if cells.n:
            rows.append({"type": "Fern-/Nahwärmeversorgung 2022", "name": f"{cells.n} versorgte 200-m-Zellen",
                         "operator": None, "distance_m": float(cells.d), "capacity_mw": None, "energy_mwh_a": None,
                         "length_km": None, "temperatures": None,
                         "detail": "Zelle gilt als versorgt, sobald eine versorgte Adresse darin liegt; Fern- und Nahwärme "
                                   "nicht getrennt, Leitungen nicht öffentlich",
                         "source": "Fern- und Nahwärmeversorgung 2022 (gdi.berlin.de)"})
            features += [{"type": "Feature", "geometry": json.loads(g), "properties": {"kind": "heat_supply"}}
                         for g in cells.gs]
    if not rows:
        notes.append(f"Im Gebiet und bis {params['dist']:.0f} m darum liegt kein bekanntes "
                     + {"heat": "Wärmenetz", "cooling": "Kältenetz"}.get(kind, "Wärme- oder Kältenetz") + ".")
    notes.append("Kältenetze: nur aus öffentlichen Angaben (in Berlin bisher Potsdamer Platz); Versorgungsgebiet "
                 "ungefähr aus den genannten Kunden, Leitungsverläufe nicht öffentlich. Distanz 0 = im Gebiet.")
    fr = frame("features", [field("type", "nominal", "Art"), field("name", "nominal", "Name"),
                            field("operator", "nominal", "Betreiber / Ansprechpartner"),
                            field("distance_m", "quantitative", "Entfernung", "m"),
                            field("capacity_mw", "quantitative", "Kälteleistung", "MW"),
                            field("energy_mwh_a", "quantitative", "Jahresarbeit", "MWh/a"),
                            field("length_km", "quantitative", "Trassenlänge", "km"),
                            field("temperatures", "nominal", "Vor-/Rücklauf"), field("detail", "nominal", "Hinweis"),
                            field("source", "nominal", "Quelle")],
               rows, ["heat_networks", "cooling_networks"], total=len(rows),
               query={"kind": kind, "max_distance_m": params["dist"], "districts": districts, "scope": scope,
                      "address": address, "place": place}, notes=notes)
    fr["geo"] = True
    return {"layer": "networks", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr}


def block_electricity(db: Session, bbox: str | None, polygon: dict | None = None, *, color_by: str = "electricity",
                      top_n: int | None = None, climate: str | None = None, year: int | None = None,
                      measures: list[str] | None = None, setpoint: float | None = None,
                      districts: list[str] | None = None, scope: str | None = None, address: str | None = None,
                      place: str | None = None, **_) -> dict:
    color_by = color_by if color_by in ("cooling_share", "cooling_demand_share") else "electricity"
    sc = cooling.scenario(climate, year, measures, setpoint)
    params: dict = {}
    cooling.bind(db, params, sc)
    area, notes = _search_area(db, params, bbox, polygon, districts, scope, address, place)
    where = f"AND ST_Intersects(blk.geom, ({area}))" if area else ""
    rows = db.execute(text(f"""
        WITH el AS (SELECT blk_id, consumption FROM emc.consumption_block WHERE energy_type = 'elec' AND year = 2021),
        bl AS (SELECT blk.blk_id, blk.geom, el.consumption FROM emc.block blk JOIN el ON el.blk_id = blk.blk_id
               WHERE true {where}),
        cool AS (SELECT b.mostly_in_block AS blk_id, SUM(cact.electricity_kwh_mean) AS act, COUNT(cact.uuid) AS n_act,
                        SUM({cooling.ELEC}) AS dem
                 FROM emc.building b
                 LEFT JOIN core_bldg.cooling_estimate_building cact ON cact.uuid = b.uuid
                 {cooling.COOL_JOIN}
                 WHERE b.mostly_in_block IN (SELECT blk_id FROM bl)
                 GROUP BY 1)
        SELECT bl.blk_id, bl.consumption, cool.act, cool.n_act, cool.dem,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(bl.geom, 2), 4326), 6) AS g
        FROM bl LEFT JOIN cool ON cool.blk_id = bl.blk_id"""), params).all()

    def mwh(v):
        return round(float(v) / 1000, 1) if v is not None else None

    out, features = [], []
    for r in rows:
        el = float(r.consumption or 0)
        row = {"block": str(r.blk_id), "electricity_mwh_a": mwh(r.consumption),
               "cooling_actual_mwh_a": mwh(r.act), "buildings_with_chiller": r.n_act or 0,
               "cooling_share_pct": round(100 * float(r.act) / el, 1) if r.act and el else None,
               "cooling_demand_mwh_a": mwh(r.dem),
               "cooling_demand_share_pct": round(100 * float(r.dem) / el, 1) if r.dem and el else None}
        out.append(row)
        v = {"electricity": row["electricity_mwh_a"], "cooling_share": row["cooling_share_pct"],
             "cooling_demand_share": row["cooling_demand_share_pct"]}[color_by]
        if v:
            features.append({"type": "Feature", "geometry": json.loads(r.g), "properties": {"block": row["block"], "value": v}})
    tot = {k: sum(r[k] or 0 for r in out) for k in ("electricity_mwh_a", "cooling_actual_mwh_a", "cooling_demand_mwh_a")}
    out.sort(key=lambda r: -(r["electricity_mwh_a"] or 0))
    top = max(1, min(int(top_n or 50), 500))
    summary = {"blocks": len(out), "electricity_mwh_a": round(tot["electricity_mwh_a"]),
               "cooling_actual_mwh_a": round(tot["cooling_actual_mwh_a"]),
               "cooling_actual_share_pct": round(100 * tot["cooling_actual_mwh_a"] / tot["electricity_mwh_a"], 1) if tot["electricity_mwh_a"] else None,
               "cooling_demand_electricity_mwh_a": round(tot["cooling_demand_mwh_a"]),
               "cooling_demand_share_pct": round(100 * tot["cooling_demand_mwh_a"] / tot["electricity_mwh_a"], 1) if tot["electricity_mwh_a"] else None}
    notes += [f"{len(out)} Blöcke: Stromverbrauch 2021 {summary['electricity_mwh_a']:,} MWh/a; davon erkannte Kälteanlagen "
              f"{summary['cooling_actual_mwh_a']:,} MWh/a ({summary['cooling_actual_share_pct']} %); wenn der ganze "
              f"Kältebedarf mit Kälteanlagen gedeckt würde: {summary['cooling_demand_electricity_mwh_a']:,} MWh/a "
              f"({summary['cooling_demand_share_pct']} %).".replace(",", "."),
              "Stromverbrauch je Block: Umweltatlas Berlin 08.10 (Netzbetreiber, 2021), alle Verbraucher zusammen, keine "
              "Aufteilung nach Haushalt/Gewerbe. Kältestrom aus erkannten Anlagen und aus dem Bedarf: Schätzungen.",
              cooling.note(sc)]
    fr = frame("aggregate", [field("block", "nominal", "Block"), field("electricity_mwh_a", "quantitative", "Stromverbrauch 2021", "MWh/a"),
                             field("cooling_actual_mwh_a", "quantitative", "Kältestrom erkannter Anlagen", "MWh/a"),
                             field("buildings_with_chiller", "quantitative", "Gebäude mit erkannter Anlage", "Gebäude"),
                             field("cooling_share_pct", "quantitative", "Anteil Kältestrom (erkannt)", "%"),
                             field("cooling_demand_mwh_a", "quantitative", "Kältestrom bei Deckung des Bedarfs", "MWh/a"),
                             field("cooling_demand_share_pct", "quantitative", "Anteil Kältestrom (Bedarf)", "%")],
               out[:top], ["block_electricity", "chillers", "chiller_estimate", "cooling_mockup"], total=len(out),
               query={"color_by": color_by, **sc, "districts": districts, "scope": scope, "address": address,
                      "place": place}, notes=notes)
    # the unit of the colouring travels with the features (the chat passes the geojson through unchanged)
    return {"layer": "cells", "geojson": {"type": "FeatureCollection", "features": features,
                                          "legend_unit": "MWh/a" if color_by == "electricity" else "%"}, "frame": fr,
            "summary_for_llm": {**summary, "top_blocks": out[:5], "notes": notes}}


def cooling_surplus(db: Session, bbox: str | None, polygon: dict | None = None, *, radius_m: float | None = None,
                    top_n: int | None = None, climate: str | None = None, year: int | None = None,
                    measures: list[str] | None = None, setpoint: float | None = None,
                    districts: list[str] | None = None, scope: str | None = None, address: str | None = None,
                    place: str | None = None, **_) -> dict:
    sc = cooling.scenario(climate, year, measures, setpoint)
    params: dict = {"radius": float(radius_m or 300), "top": max(1, min(int(top_n or 20), 200))}
    cooling.bind(db, params, sc)
    area, notes = _search_area(db, params, bbox, polygon, districts, scope, address, place)
    where = f"AND ST_Intersects(b.geom, ({area}))" if area else ""
    rows = db.execute(text(f"""
        WITH s AS (
            SELECT b.uuid, b.geom, b.namlag, b.hnr, kg.gfk_name AS bf,
                   cact.capacity_kw_mean AS inst, cact.capacity_kw_min AS inst_min, cact.capacity_kw_max AS inst_max,
                   COALESCE({cooling.CAPACITY}, 0) AS req, cact.n_units
            FROM core_bldg.cooling_estimate_building cact
            JOIN emc.building b ON b.uuid = cact.uuid
            LEFT JOIN core_bldg.kww_gfk_mapping kg ON kg.gfk = b.gfk
            {cooling.COOL_JOIN}
            -- only heated buildings whose chillers serve the building itself: no process cooling (production,
            -- cold stores, data centres, storage halls), there the installed capacity is used by the process, not spare
            WHERE cb.uuid IS NOT NULL AND cb.cooling_group NOT IN ('produktion_gewerbe', 'kuehlhaus', 'rechenzentrum', 'lager')
              AND cact.capacity_kw_mean > COALESCE({cooling.CAPACITY}, 0) {where}
            ORDER BY cact.capacity_kw_mean - COALESCE({cooling.CAPACITY}, 0) DESC LIMIT :top)
        SELECT s.*, s.inst - s.req AS surplus, nb.n, nb.req_kw, nb.dem_kwh, nb.d_min,
               ST_AsGeoJSON(ST_Transform(s.geom, 4326), 6) AS g
        FROM s CROSS JOIN LATERAL (
            SELECT count(*) AS n, SUM({cooling.CAPACITY}) AS req_kw, SUM({cooling.ANNUAL}) AS dem_kwh,
                   min(ST_Distance(b.geom, s.geom)) AS d_min
            FROM emc.building b {cooling.COOL_JOIN}
            WHERE ST_DWithin(b.geom, s.geom, :radius) AND b.uuid <> s.uuid AND cb.uuid IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM core_bldg.cooling_estimate_building x WHERE x.uuid = b.uuid)) nb
        ORDER BY surplus DESC"""), params).all()
    out, features = [], []
    for k, r in enumerate(rows):
        address_s = r.namlag if r.namlag and r.hnr and r.namlag.endswith(r.hnr) else " ".join(x for x in (r.namlag, r.hnr) if x) or None
        out.append({"rank": k + 1, "bldg_uuid": r.uuid, "address": address_s, "building_function": r.bf,
                    "installed_kw": round(float(r.inst)), "installed_min_kw": round(float(r.inst_min or 0)),
                    "installed_max_kw": round(float(r.inst_max or 0)), "required_kw": round(float(r.req)),
                    "surplus_kw": round(float(r.surplus)), "neighbours": r.n,
                    "neighbours_required_kw": round(float(r.req_kw or 0)),
                    "neighbours_demand_mwh_a": round(float(r.dem_kwh or 0) / 1000, 1),
                    "nearest_neighbour_m": round(float(r.d_min)) if r.d_min is not None else None,
                    "covers_neighbours_pct": round(min(100, 100 * float(r.surplus) / float(r.req_kw))) if r.req_kw else None})
        features.append({"type": "Feature", "geometry": json.loads(r.g),
                         "properties": {"bldg_uuid": r.uuid, "rank": k + 1, "value": round(float(r.surplus))}})
    notes += [f"Kälteüberschuss = installierte Leistung erkannter Anlagen (KIT-Schätzung, Mittel) minus benötigte "
              f"Leistung des Gebäudes; Nachbarn = beheizte Gebäude ohne erkannte Anlage im Umkreis von "
              f"{params['radius']:.0f} m. Ohne Prozesskälte (Produktion, Kühlhaus, Lager, Rechenzentrum).",
              "Große Unsicherheit: die installierte Leistung enthält Reserve/Überdimensionierung, die zeitliche "
              "Verfügbarkeit (Lastgänge) und die Nutzung durch den Eigentümer sind unbekannt.", cooling.note(sc)]
    fr = frame("features", [field("rank", "ordinal", "Rang"), field("bldg_uuid", "id", "Gebäude-ID"),
                            field("address", "nominal", "Adresse"), field("building_function", "nominal", "Gebäudefunktion (ALKIS)"),
                            field("installed_kw", "quantitative", "installierte Kälteleistung", "kW"),
                            field("installed_min_kw", "quantitative", "installiert min", "kW"),
                            field("installed_max_kw", "quantitative", "installiert max", "kW"),
                            field("required_kw", "quantitative", "benötigte Kälteleistung", "kW"),
                            field("surplus_kw", "quantitative", "Kälteüberschuss", "kW"),
                            field("neighbours", "quantitative", "Nachbarn ohne Anlage", "Gebäude"),
                            field("neighbours_required_kw", "quantitative", "deren benötigte Leistung", "kW"),
                            field("neighbours_demand_mwh_a", "quantitative", "deren Kältebedarf", "MWh/a"),
                            field("nearest_neighbour_m", "quantitative", "nächster Nachbar", "m"),
                            field("covers_neighbours_pct", "quantitative", "Überschuss deckt Nachbarn zu", "%")],
               out, ["chillers", "chiller_estimate", "cooling_mockup"], ids=[r["bldg_uuid"] for r in out], total=len(out),
               query={"radius_m": params["radius"], **sc, "districts": districts, "scope": scope, "address": address,
                      "place": place}, notes=notes)
    return {"layer": "buildings", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr,
            "summary_for_llm": {"buildings": out[:10], "notes": notes}}


def building_scenario_demand(db: Session, bbox: str | None, polygon: dict | None = None, *,
                             compare_measures: list[str] | None = None, climate: str | None = None,
                             year: int | None = None, setpoint: float | None = None, top_n: int | None = None,
                             districts: list[str] | None = None, scope: str | None = None, address: str | None = None,
                             place: str | None = None, **_) -> dict:
    compare = [m for m in (compare_measures or []) if m in cooling.MEASURES]
    std = cooling.scenario(climate, year, None, setpoint)
    # without = the scenario's measures minus the asked ones; with = plus the asked ones
    sc_without = {**std, "measures": [m for m in std["measures"] if m not in compare]}
    sc_with = {**std, "measures": sorted(set(sc_without["measures"]) | set(compare))}
    top = max(1, min(int(top_n or 10), 500))
    params: dict = {"top": top}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False)
    where_area += _filter_sql(params, None, None, None, db, address=address, place=place)
    cooling.bind(db, params, sc_without)
    # measures don't act on data centres and cold stores (process cooling): rank only buildings where they can act
    rows = db.execute(text(f"""
        SELECT b.uuid, b.namlag, b.hnr, b.bezgfk, cb.age_class, cb.ngf_m2,
               {cooling.ANNUAL} AS v_std, {cooling.ANNUAL_MIN} AS v_min, {cooling.ANNUAL_MAX} AS v_max,
               ST_AsGeoJSON(ST_Transform(b.geom, 4326), 6) AS geom
        {_BASE} {join}
        WHERE {"true" if (address or place) else "cb.measures_apply"} {where_area}
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
    notes.append("Rechenzentren und Kühlhäuser (Prozesskälte): die Maßnahmen wirken dort nicht"
                 + ("." if (address or place) else ", deshalb sind sie nicht in der Rangfolge."))
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
                 scope: str | None = None, climate: str | None = None, year: int | None = None,
                 measures: list[str] | None = None, setpoint: float | None = None, **_) -> dict:
    carrier = "heat" if carrier == "heat" else "cooling"
    scenario = scenario if scenario in LINE_DENSITY_LABEL else "standard"
    src, custom_label = "core_base.v_line_density", None
    if any(v is not None for v in (climate, year, measures, setpoint)):
        # any scenario: a precomputed one if it matches, else computed now (the whole city, ~0.5 min)
        sc = cooling.scenario(climate, year, measures, setpoint)
        hit = db.execute(text("""SELECT scenario FROM core_bldg.line_density_scenario
                                 WHERE climate = :cl AND year = :yr AND sort IS NOT NULL
                                   AND measures @> CAST(:ms AS text[]) AND measures <@ CAST(:ms AS text[])
                                   AND setpoint IS NOT DISTINCT FROM CAST(:sp AS numeric)"""),
                         {"cl": sc["climate"], "yr": sc["year"], "ms": sc["measures"], "sp": sc["setpoint"]}).scalar()
        if hit:
            scenario = hit
        else:
            scenario, custom_label = "custom", cooling.label(sc)
            src = ("(SELECT 'custom'::text AS scenario, f.*, s.street_name, s.district, s.geom "
                   "FROM core_bldg.f_line_density(:cl, :yr, CAST(:ms AS text[]), CAST(:sp AS numeric), '2045_o45') f "
                   "JOIN core_base.street_segment s ON s.id = f.segment_id)")
    trasse = basis != "street"
    by_street = group_by == "street"
    top = max(1, min(int(top_n or 300), 2000))
    params: dict = {"scenario": scenario, "top": top, "min_len": float(min_length_m or 0),
                    "min_n": int(min_buildings or 0)}
    if custom_label:
        params.update({"cl": sc["climate"], "yr": sc["year"], "ms": sc["measures"], "sp": sc["setpoint"]})
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
            FROM {src} l
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
        FROM {src} l WHERE {' AND '.join(where)} AND l.street_m >= :min_len"""), params).one()
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
    notes = [f"Szenario {(custom_label or LINE_DENSITY_LABEL[scenario])}. Liniendichte = Jahresbedarf der angeschlossenen beheizten "
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
                                "scenario": (custom_label or LINE_DENSITY_LABEL[scenario]), "top": out[:10],
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


# ------------------------------------------------------------------------------------------------------------
# Placeholder tools (2026-11-20): street trees, indoor overheating, retrofit path. The numbers are assumptions
# or made up until the project's simulations (SimStadt) arrive; the tools give the structure for those answers.
# ------------------------------------------------------------------------------------------------------------

TREE_PLACEHOLDER = {
    "air_cooling_k": 3.0,           # cooling of the ambient air (made up, to be replaced)
    "shade_pct": (-15, -8, -3),     # cooling demand of a shaded building, min/std/max % (made up)
    "shade_distance_m": 12,         # a building counts as shaded with a new tree this close (crown radius + gap)
    "crown_m": 8, "height_m": 12,   # drawn size of a new tree (grown for about 20 years)
}
PLACEHOLDER_NOTE = "Hinweis: Dies sind ausgedachte Zahlen; sie werden bald durch Simulationsergebnisse ersetzt."
OWNER_LABEL = {"land_berlin": "landeseigene Grundstücke", "bezirk": "Bezirksverwaltung",
               "hauptverwaltung": "Hauptverwaltung", "sondervermoegen": "Sondervermögen/Liegenschaftsfonds",
               "nicht_landeseigen": "nicht landeseigen"}


def _address_text(r) -> str | None:
    return r.namlag if r.namlag and r.hnr and r.namlag.endswith(r.hnr) else \
        " ".join(x for x in (r.namlag, r.hnr) if x) or None


def street_trees_scenario(db: Session, bbox: str | None, polygon: dict | None = None, *, street: str | None = None,
                          spacing_m: float | None = None, districts: list[str] | None = None,
                          address: str | None = None, place: str | None = None, climate: str | None = None,
                          year: int | None = None, setpoint: float | None = None, **_) -> dict:
    sp = max(5.0, min(float(spacing_m or 15), 50.0))
    params: dict = {"sp": sp, "off": 7.0, "gap": 5.0, "eps": sp * 0.4, "shade": TREE_PLACEHOLDER["shade_distance_m"]}
    where = ["coalesce(s.class2, '') NOT IN ('AUTO', 'BAB')", "coalesce(s.class, '') <> 'A'"]
    clip = "s.geom"
    notes: list[str] = []
    if polygon:
        # a drawn line or point: the street sections within 20 m of it; a drawn polygon: the sections inside
        params["polygon_geojson"] = json.dumps(polygon)
        g = "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:polygon_geojson), 4326), 25833)"
        area = g if polygon.get("type") == "Polygon" else f"ST_Buffer({g}, 20)"
        where.append(f"ST_Intersects(s.geom, {area})")
        clip = f"ST_Intersection(s.geom, {area})"
        what = "markierter Abschnitt"
    elif street:
        params["street"] = f"%{street.strip()}%"
        where.append("s.street_name ILIKE :street")
        if districts:
            params["dist0"] = f"%{districts[0]}%"
            where.append("s.district ILIKE :dist0")
        what = street
    elif address or place:
        area, notes = _search_area(db, params, None, None, None, None, address, place)
        area = f"ST_Buffer(({area}), 40)"
        where.append(f"ST_Intersects(s.geom, {area})")
        clip = f"ST_Intersection(s.geom, {area})"
        what = address or place
    else:
        return {"error": "Kein Straßenabschnitt: bitte in der Karte eine Linie entlang der Straße zeichnen oder eine "
                         "Straße (mit Bezirk) bzw. Adresse nennen."}
    cond = " AND ".join(where)
    rows = db.execute(text(f"""
        WITH seg AS (
            SELECT s.id, s.street_name,
                   (ST_Dump(ST_LineMerge(ST_CollectionExtract(ST_Multi({clip}), 2)))).geom AS g
            FROM core_base.street_segment s WHERE {cond}),
        side AS (
            SELECT id, street_name, (ST_Dump(ST_OffsetCurve(g, o))).geom AS g
            FROM seg, (VALUES (CAST(:off AS float8)), (-CAST(:off AS float8))) v(o) WHERE ST_Length(g) > 2),
        pts AS (
            SELECT id, street_name, ST_LineInterpolatePoint(g, 0.5) AS p FROM side
             WHERE ST_Length(g) BETWEEN :sp * 0.5 AND :sp
            UNION ALL
            SELECT id, street_name, (ST_Dump(ST_LineInterpolatePoints(g, :sp / ST_Length(g), true))).geom
              FROM side WHERE ST_Length(g) > :sp),
        free AS (
            SELECT p.* FROM pts p
            WHERE NOT EXISTS (SELECT 1 FROM core_veg.strassenbaeume t WHERE ST_DWithin(t.geom, p.p, :gap))
              AND NOT EXISTS (SELECT 1 FROM core_veg.anlagenbaeume t WHERE ST_DWithin(t.geom, p.p, :gap))
              AND NOT EXISTS (SELECT 1 FROM emc.building b WHERE b.displayed AND ST_DWithin(b.geom, p.p, 2))),
        cl AS (SELECT *, ST_ClusterDBSCAN(p, :eps, 1) OVER () AS c FROM free)
        SELECT DISTINCT ON (c) street_name, ST_X(p) AS x, ST_Y(p) AS y,
               ST_X(ST_Transform(p, 4326)) AS lon, ST_Y(ST_Transform(p, 4326)) AS lat
        FROM cl ORDER BY c, id LIMIT 5000"""), params).all()
    if not rows:
        return {"error": f"Für '{what}' wurden keine Straßenabschnitte mit freiem Platz für neue Bäume gefunden."}
    n_existing = db.execute(text(f"""
        SELECT (SELECT count(*) FROM core_veg.strassenbaeume t WHERE ST_DWithin(t.geom, a.g, 15))
             + (SELECT count(*) FROM core_veg.anlagenbaeume t WHERE ST_DWithin(t.geom, a.g, 15)) AS n
        FROM (SELECT ST_Union({clip}) AS g FROM core_base.street_segment s WHERE {cond}) a"""), params).scalar() or 0

    sc = cooling.scenario(climate, year, None, setpoint)
    p2 = cooling.bind(db, {"xs": [r.x for r in rows], "ys": [r.y for r in rows], "shade": params["shade"]}, sc)
    shaded = db.execute(text(f"""
        WITH t AS (SELECT ST_SetSRID(ST_MakePoint(x, y), 25833) AS p
                   FROM unnest(CAST(:xs AS float8[]), CAST(:ys AS float8[])) u(x, y)),
        hit AS (SELECT DISTINCT b.uuid FROM t JOIN emc.building b ON b.displayed AND ST_DWithin(b.geom, t.p, :shade))
        SELECT b.uuid, {cooling.ANNUAL} AS v
        FROM hit JOIN emc.building b ON b.uuid = hit.uuid {cooling.COOL_JOIN}
        WHERE coalesce(cb.measures_apply, false)"""), p2).all()
    lo, mid, hi = TREE_PLACEHOLDER["shade_pct"]
    demand = sum(float(r.v or 0) for r in shaded) / 1000
    save = {"min": round(demand * -hi / 100, 1), "std": round(demand * -mid / 100, 1), "max": round(demand * -lo / 100, 1)}

    per_street: dict[str, int] = {}
    for r in rows:
        key = r.street_name or "ohne Namen"
        per_street[key] = per_street.get(key, 0) + 1
    out = [{"street": k, "new_trees": v} for k, v in sorted(per_street.items(), key=lambda kv: -kv[1])]
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(r.lon, 7), round(r.lat, 7)]},
                 "properties": {"art_dtsch": "Szenario: neuer Straßenbaum", "art_gruppe": "Laubbaum",
                                "herkunft": "szenario", "strasse": r.street_name, "krone_m": TREE_PLACEHOLDER["crown_m"],
                                "hoehe_m": TREE_PLACEHOLDER["height_m"], "stammumfg": 60, "szenario": True}}
                for r in rows]
    features += [{"type": "Feature", "geometry": None, "properties": {"bldg_uuid": r.uuid}} for r in shaded]
    text_de = (f"Es würden {len(rows)} Bäume gepflanzt werden (alle {sp:.0f} m auf beiden Straßenseiten, wo kein Baum "
               f"und kein Gebäude im Weg ist; {n_existing} Bäume stehen dort schon). Sie kühlen die Umgebungsluft um "
               f"{TREE_PLACEHOLDER['air_cooling_k']:.0f} °C ab. Sie verschatten die Fassaden von {len(shaded)} Gebäuden "
               f"und senken deren Kältebedarf um etwa {save['std']:.1f} MWh/a ({-mid} %, Spanne {-hi} bis {-lo} %)."
               ).replace(f"{save['std']:.1f} MWh", f"{save['std']:.1f} MWh".replace(".", ","))
    notes += [text_de, PLACEHOLDER_NOTE,
              f"Kältebedarf der beschatteten Gebäude: {cooling.label(sc)}; {demand:.1f} MWh/a ohne die neuen Bäume.".replace(
                  f"{demand:.1f}", f"{demand:.1f}".replace(".", ",")),
              "Die neuen Bäume sind nur ein Szenario in der Karte (lila) und werden nicht gespeichert. Straßenbreite, "
              "Leitungen im Boden, Zufahrten und Parkplätze sind nicht geprüft."]
    fr = frame("aggregate", [field("street", "nominal", "Straße"), field("new_trees", "quantitative", "neue Bäume")],
               out, ["street_tree_scenario", "street_network", "trees", "cooling_mockup"], total=len(out),
               query={"street": street, "spacing_m": sp, "districts": districts, "address": address, "place": place,
                      **sc}, notes=notes)
    return {"layer": "scenario_trees", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr,
            "summary_for_llm": {"new_trees": len(rows), "existing_trees_nearby": n_existing, "spacing_m": sp,
                                "air_cooling_k": TREE_PLACEHOLDER["air_cooling_k"], "shaded_buildings": len(shaded),
                                "cooling_demand_shaded_mwh_a": round(demand, 1), "saving_mwh_a": save,
                                "saving_pct_std": -mid, "per_street": out[:10], "text": text_de,
                                "placeholder_note": PLACEHOLDER_NOTE}}


# production, storage and process cooling are not what "public building" means in a retrofit path
NO_RETROFIT = ["produktion_gewerbe", "lager", "kuehlhaus", "rechenzentrum"]
PASSIVE = ["verschattung_aussen", "lueftung_nutzer", "nachtlueftung_auto", "fenstertausch"]
INDOOR_JOIN = """
    JOIN core_bldg.indoor_base_building ib ON ib.uuid = b.uuid
    JOIN core_bldg.indoor_lookup il ON il.ref = ib.ref AND il.threshold_c = :th
         AND il.delta_k = round(greatest(-6, least(8, ib.base_delta_k + CAST(:d AS numeric))), 1)"""


def _indoor_delta(db: Session, sc: dict) -> float:
    return float(db.execute(text("SELECT core_bldg.f_indoor_climate_delta(:cl, :yr) "
                                 "+ core_bldg.f_indoor_measure_delta(CAST(:ms AS text[]))"),
                            {"cl": sc["climate"], "yr": sc["year"], "ms": sc["measures"]}).scalar() or 0)


def indoor_overheating(db: Session, bbox: str | None, polygon: dict | None = None, *, threshold_c: int | None = None,
                       min_days: int | None = None, compare_measures: bool | None = None,
                       climate: str | None = None, year: int | None = None, measures: list[str] | None = None,
                       top_n: int | None = None, districts: list[str] | None = None, scope: str | None = None,
                       postcodes: list[str] | None = None, usage_zone_type: str | None = None,
                       min_year_built: int | None = None, max_year_built: int | None = None,
                       building_function: str | None = None, address: str | None = None, place: str | None = None,
                       **extra) -> dict:
    th = max(26, min(int(threshold_c or 30), 32))
    nd = max(0, int(min_days if min_days is not None else 10))
    sc = cooling.scenario(climate, year, measures, None)
    params: dict = {"th": th, "nd": nd, "top": max(1, min(int(top_n or 20), 500))}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False, postcodes)
    where_area += _filter_sql(params, usage_zone_type, min_year_built, max_year_built, db,
                              building_function=building_function, address=address, place=place, **extra)
    srcs = ["indoor_mockup", "cooling_mockup", "climate_model", "alkis_function"]
    base_notes = [f"Überhitzung innen: Tage, an denen die Raumtemperatur (operativ) ohne Kühlung über {th} °C steigt, "
                  f"und Stunden über 26 °C. Klima: {cooling.CLIMATES[sc['climate']]}, {sc['year']}.",
                  "Platzhalter: Referenzläufe aus citygml2msl (Wohnen, Büro, Krankenhaus; Wetter 2023) pauschal "
                  "verschoben um Klima, Maßnahmen, Baualter und Wärmebelastung am Standort; die SimStadt-Simulationen "
                  "ersetzen das. Gewerbe, Lager, Schwimmbäder, Kühlhäuser und Rechenzentren sind nicht enthalten.",
                  PLACEHOLDER_NOTE]

    if compare_measures:
        # one building (address/place) or a small selection: which measure combination keeps it below the limit
        combos = [[]] + [[m] for m in cooling.MEASURES] + [PASSIVE, PASSIVE + ["sanierung"]]
        rows = []
        for ms in combos:
            params["d"] = _indoor_delta(db, {**sc, "measures": ms})
            r = db.execute(text(f"""
                SELECT count(*) AS n, max(il.days_over) AS days, max(il.hours_over_26) AS h26, max(il.max_op_c) AS mx
                {_BASE} {INDOOR_JOIN} {join} WHERE true {where_area}"""), params).one()
            if not r.n:
                return {"error": "Für diese Gebäude gibt es keine Schätzung der Innentemperatur (nur Wohnen, Büro, "
                                 "Bildung, Handel, Kultur, Sport, Hotel und Krankenhaus)."}
            if len(ms) >= len(PASSIVE):
                name = "alle passiven Maßnahmen" + (" + Sanierung der Hülle" if "sanierung" in ms else "")
            else:
                name = " + ".join(cooling.MEASURE_LABEL[m] for m in ms) or "ohne Maßnahmen"
            rows.append({"measures": name, "days_over": r.days, "hours_over_26": r.h26,
                         "max_op_c": float(r.mx), "natural_enough": "ja" if (r.days or 0) <= nd else "nein"})
        fr = frame("aggregate", [field("measures", "nominal", "Maßnahmen"),
                                 field("days_over", "quantitative", f"Tage über {th} °C"),
                                 field("hours_over_26", "quantitative", "Stunden über 26 °C", "h"),
                                 field("max_op_c", "quantitative", "höchste Raumtemperatur", "°C"),
                                 field("natural_enough", "nominal", f"ohne Kühlung ausreichend (höchstens {nd} Tage)")],
                   rows, srcs, total=len(rows),
                   query={"threshold_c": th, "min_days": nd, "compare_measures": True, "climate": sc["climate"],
                          "year": sc["year"], "address": address, "place": place},
                   notes=base_notes + ["Bei mehreren Gebäuden zählt das ungünstigste. Die Sanierung der Hülle "
                                       "erhöht die Überhitzung leicht (Dämmung hält Wärme im Gebäude)."])
        return {"frame": fr, "summary_for_llm": {"rows": rows, "threshold_c": th, "min_days": nd,
                                                 "notes": base_notes}}

    params["d"] = _indoor_delta(db, sc)
    rows = db.execute(text(f"""
        SELECT b.uuid, b.namlag, b.hnr, b.bezgfk, cb.age_class, il.days_over, il.hours_over_26, il.max_op_c
        {_BASE} {INDOOR_JOIN} {join}
        WHERE true {where_area}
        ORDER BY il.days_over DESC, il.hours_over_26 DESC LIMIT 40000"""), params).all()
    n = len(rows)
    over = [r for r in rows if (r.days_over or 0) > nd]
    out = [{"rank": k + 1, "bldg_uuid": r.uuid, "address": _address_text(r), "building_function": r.bezgfk,
            "age_class": r.age_class, "days_over": r.days_over, "hours_over_26": r.hours_over_26,
            "max_op_c": float(r.max_op_c)} for k, r in enumerate(rows[:params["top"]])]
    features = [{"type": "Feature", "geometry": None, "properties": {"bldg_uuid": r.uuid, "value": r.days_over}}
                for r in rows if r.days_over]
    notes = base_notes + [f"Maßnahmen: {', '.join(cooling.MEASURE_LABEL[m] for m in sc['measures']) or 'keine'}. "
                          f"{len(over)} von {n} Gebäuden liegen über {nd} Tagen: dort wäre eine Kühlung "
                          f"(Klimaanlage) unvermeidlich."]
    fr = frame("features", [field("rank", "ordinal", "Rang"), field("bldg_uuid", "id", "Gebäude-ID"),
                            field("address", "nominal", "Adresse"),
                            field("building_function", "nominal", "Gebäudefunktion (ALKIS)"),
                            field("age_class", "ordinal", "Baualtersklasse"),
                            field("days_over", "quantitative", f"Tage über {th} °C"),
                            field("hours_over_26", "quantitative", "Stunden über 26 °C", "h"),
                            field("max_op_c", "quantitative", "höchste Raumtemperatur", "°C")],
               out, srcs, ids=[r["bldg_uuid"] for r in out], total=n,
               query={"threshold_c": th, "min_days": nd, **sc, "districts": districts, "scope": scope,
                      "address": address, "place": place, "usage_zone_type": usage_zone_type,
                      "building_function": building_function, **{k: v for k, v in extra.items() if v is not None}},
               notes=notes)
    return {"layer": "building_values_warm",
            "geojson": {"type": "FeatureCollection", "features": features, "legend_unit": "Tage"}, "frame": fr,
            "summary_for_llm": {"buildings": n, "buildings_over_min_days": len(over), "threshold_c": th,
                                "min_days": nd, "measures": sc["measures"], "top": out[:10], "notes": notes}}


def retrofit_path(db: Session, bbox: str | None, polygon: dict | None = None, *, interval_years: int | None = None,
                  per_step: int | None = None, start_year: int | None = None, end_year: int | None = None,
                  order: str | None = None, owner: str | None = None, climate: str | None = None,
                  measures: list[str] | None = None, setpoint: float | None = None,
                  districts: list[str] | None = None, scope: str | None = None, postcodes: list[str] | None = None,
                  usage_zone_type: str | None = None, building_function: str | None = None,
                  address: str | None = None, place: str | None = None, **extra) -> dict:
    iv = max(1, min(int(interval_years or 4), 20))
    k_step = max(1, min(int(per_step or 1), 100))
    y0 = max(2025, min(int(start_year or 2026), 2050))
    y1 = max(y0, min(int(end_year or 2045), 2050))
    owner = owner or "land_berlin"
    sc = cooling.scenario(climate, 2025, measures, setpoint)
    sc["measures"] = [m for m in sc["measures"] if m != "sanierung"]
    params: dict = {}
    join, where_area = _area_sql(params, bbox, polygon, districts, scope, False, postcodes)
    area_only = where_area + _filter_sql(params, None, None, None, db, address=address, place=place)
    cooling.bind(db, params, sc)  # year 2025; the other years scale with the climate factor
    total25 = float(db.execute(text(f"SELECT sum({cooling.ANNUAL}) {_BASE} {join} WHERE true {area_only}"),
                               params).scalar() or 0)
    cand_where = where_area + _filter_sql(params, usage_zone_type, None, None, db,
                                          building_function=building_function, address=address, place=place,
                                          owner=owner, **extra)
    year_sql = 'CASE WHEN pb."Year of construction" ~ \'^[0-9]+$\' THEN pb."Year of construction"::int END'
    ordering = f"{year_sql} NULLS LAST, v DESC" if order == "oldest" else "v DESC NULLS LAST"
    n_steps = (y1 - y0) // iv + 1
    cands = db.execute(text(f"""
        SELECT b.uuid, b.namlag, b.hnr, b.bezgfk, cb.age_class, {cooling.ANNUAL} AS v
        {_BASE} {join}
        WHERE cb.measures_apply AND {cooling.ANNUAL} > 0 AND cb.cooling_group <> ALL(:no_retrofit) {cand_where}
        ORDER BY {ordering} LIMIT :lim"""), {**params, "lim": n_steps * k_step, "no_retrofit": NO_RETROFIT}).all()
    sel = [(y0 + (i // k_step) * iv, r) for i, r in enumerate(cands)]
    pct = db.execute(text("SELECT cool_pct_min, cool_pct_std, cool_pct_max FROM core_bldg.demand_measure "
                          "WHERE measure = 'sanierung'")).one()
    c25 = float(params["c_cool"])
    cf = {r.y: float(r.c) for r in db.execute(text(
        "SELECT y, core_bldg.f_climate_factor(:cl, y, 'cool') AS c FROM generate_series(2025, :y1) y"),
        {"cl": sc["climate"], "y1": y1}).all()}
    rows = []
    for y in range(2025, y1 + 1):
        scale = cf[y] / c25 if c25 else 1
        done = [r for (yr, r) in sel if yr <= y]
        delta = {b: sum(float(r.v) for r in done) * scale * float(getattr(pct, f"cool_pct_{b}")) / 100
                 for b in ("min", "std", "max")}
        base = total25 * scale / 1000
        rows.append({"year": y, "year_label": str(y), "variant": "ohne Sanierungen", "value": round(base, 1),
                     "value_min": None, "value_max": None, "renovated": 0})
        rows.append({"year": y, "year_label": str(y), "variant": "mit Sanierungspfad",
                     "value": round(base + delta["std"] / 1000, 1),
                     "value_min": round(base + delta["min"] / 1000, 1),
                     "value_max": round(base + delta["max"] / 1000, 1), "renovated": len(done)})
    plan = [{"year": yr, "bldg_uuid": r.uuid, "address": _address_text(r), "building_function": r.bezgfk,
             "age_class": r.age_class, "demand_2025_mwh_a": round(float(r.v) / 1000, 1)} for yr, r in sel]
    last = rows[-1]
    notes = [f"Sanierungspfad: ab {y0} alle {iv} Jahre {k_step} Gebäude ({OWNER_LABEL.get(owner, owner)}, "
             f"{'älteste zuerst' if order == 'oldest' else 'höchster Kältebedarf zuerst'}), bis {y1} "
             f"{len(sel)} Gebäude (ohne Produktion, Lager und Prozesskälte). Kältebedarf des ganzen Gebiets: {cooling.CLIMATES[sc['climate']]}, Maßnahmen: "
             f"{', '.join(cooling.MEASURE_LABEL[m] for m in sc['measures']) or 'keine'}.",
             f"Sanierung der Hülle: {pct.cool_pct_std:+.0f} % Kältebedarf (Spanne {pct.cool_pct_min:+.0f} bis "
             f"{pct.cool_pct_max:+.0f} %), Pauschalwert; je nach Gebäude kann der Kältebedarf auch steigen.",
             PLACEHOLDER_NOTE]
    if not sel:
        notes.insert(0, "Im Gebiet wurde kein passendes Gebäude für den Sanierungspfad gefunden.")
    fr = frame("series", [field("year", "temporal", "Jahr"), field("variant", "nominal", "Variante"),
                          field("value", "quantitative", "Kältebedarf", "MWh/a"),
                          field("value_min", "quantitative", "min", "MWh/a"),
                          field("value_max", "quantitative", "max", "MWh/a"),
                          field("renovated", "quantitative", "sanierte Gebäude")],
               rows, ["cooling_mockup", "state_property", "alkis_function"], total=len(rows),
               query={"interval_years": iv, "per_step": k_step, "start_year": y0, "end_year": y1, "order": order,
                      "owner": owner, **sc, "districts": districts, "scope": scope, "address": address,
                      "place": place},
               notes=notes)
    features = [{"type": "Feature", "geometry": None, "properties": {"bldg_uuid": p["bldg_uuid"]}} for p in plan]
    return {"layer": "buildings", "geojson": {"type": "FeatureCollection", "features": features}, "frame": fr,
            "summary_for_llm": {"area_demand_2025_mwh_a": rows[0]["value"],
                                f"without_path_{y1}_mwh_a": rows[-2]["value"], f"with_path_{y1}_mwh_a": last["value"],
                                "with_path_range_mwh_a": [last["value_min"], last["value_max"]],
                                "renovated_buildings": plan, "notes": notes}}


def report_missing_data(db: Session, bbox: str | None, polygon: dict | None = None, *, topic: str,
                        place: str | None = None) -> dict:
    what, why = MISSING.get(topic, ("Diese Daten", "sind noch nicht im Datenbestand"))
    if topic == "other_city" and place:
        what = f"Gebäudedaten für {place}"
    return frame("none", [], [], [], total=0, query={"topic": topic, "place": place},
                 notes=[f"Fehlt noch: {what}. {why[0].upper()}{why[1:]}."])


def execute(name: str, tool_input: dict, db: Session, bbox: str | None, polygon: dict | None) -> dict | None:
    """Run one of this module's tools, or None if the name isn't one of them."""
    try:
        return _execute(name, tool_input, db, bbox, polygon)
    except LookupError as e:  # address or place not found
        return {"error": str(e)}


def _execute(name: str, tool_input: dict, db: Session, bbox: str | None, polygon: dict | None) -> dict | None:
    if tool_input.get("address") or tool_input.get("place"):
        # an address or a named site replaces the map view, drawn area and districts
        tool_input = {k: v for k, v in tool_input.items() if k not in ("districts", "postcodes")}
        tool_input["scope"], polygon = "berlin", None
    if name == "cooling_scenarios":
        return cooling_scenarios(db, bbox, polygon, **tool_input)
    if name == "existing_networks":
        return existing_networks(db, bbox, polygon, **tool_input)
    if name == "heat_stress_map":
        return heat_stress_map(db, bbox, polygon, **tool_input)
    if name == "cool_places":
        return cool_places(db, bbox, polygon, **tool_input)
    if name == "block_electricity":
        return block_electricity(db, bbox, polygon, **tool_input)
    if name == "cooling_surplus":
        return cooling_surplus(db, bbox, polygon, **tool_input)
    if name == "color_buildings":
        return color_buildings(db, bbox, polygon, **tool_input)
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
    if name == "street_trees_scenario":
        return street_trees_scenario(db, bbox, polygon, **tool_input)
    if name == "indoor_overheating":
        return indoor_overheating(db, bbox, polygon, **tool_input)
    if name == "retrofit_path":
        return retrofit_path(db, bbox, polygon, **tool_input)
    if name == "report_missing_data":
        return report_missing_data(db, bbox, polygon, **tool_input)
    return None
