"""Cooling demand for the chat tools: the mock-up per building function and age class (kWh/(m²·a)) with climate,
measure and setpoint factors, the same arithmetic as core_bldg.f_building_demand.

Every tool reads core_bldg.cooling_base_building (alias cb: demand 2025 without climate change, measures or setpoint
change) and scales it with the factors of the requested scenario, bound as query parameters. Without details in the
question the standard scenario applies: medium climate change, 2030, setpoint per building function, external window
shading, no night ventilation.

The cooling estimated from installed (detected) chillers is a different thing, the "Kälteverbrauch"
(core_bldg.cooling_estimate_building); tools report it next to the demand as a cross-check.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

CLIMATES = {"gering": "geringer Klimawandel", "mittel": "mittlerer Klimawandel", "stark": "starker Klimawandel"}
MEASURES = ["verschattung_aussen", "lueftung_nutzer", "nachtlueftung_auto", "fenstertausch", "sanierung"]
SETPOINTS = (22, 24, 26)
STANDARD = {"climate": "mittel", "year": 2030, "measures": ["verschattung_aussen"], "setpoint": None}

COOL_JOIN = "LEFT JOIN core_bldg.cooling_base_building cb ON cb.uuid = b.uuid"

# JSON-schema properties shared by every tool that reports cooling demand
SCENARIO_PROPS = {
    "climate": {"type": "string", "enum": list(CLIMATES),
                "description": "Climate change scenario. Omit unless the question names one (standard: mittel)."},
    "year": {"type": "integer",
             "description": "Year 2025-2050. Omit unless the question names one (standard: 2030)."},
    "measures": {"type": "array", "items": {"type": "string", "enum": MEASURES},
                 "description": "Measures in place: verschattung_aussen (external window shading), lueftung_nutzer "
                                "(ventilation by users), nachtlueftung_auto (automatic night ventilation), fenstertausch "
                                "(new windows with solar control glazing), sanierung (energy retrofit of the envelope: "
                                "insulation, windows; can raise cooling demand). Omit unless "
                                "the question names measures (standard: verschattung_aussen). [] = no measures."},
    "setpoint": {"type": "number", "enum": list(SETPOINTS),
                 "description": "Cooling setpoint in °C. Omit unless named (standard: per building function)."},
}


def _bound_setpoint(bound: str) -> str:
    # same as core_bldg.f_setpoint_factor: min/max are the smaller/larger of the two percentage bounds
    if bound == "std":
        return "power(1 + :sp_pct_std / 100.0, :sp - cb.setpoint_base_c)"
    fn = "LEAST" if bound == "min" else "GREATEST"
    return (f"{fn}(power(1 + :sp_pct_min / 100.0, :sp - cb.setpoint_base_c), "
            f"power(1 + :sp_pct_max / 100.0, :sp - cb.setpoint_base_c))")


def demand_expr(bound: str = "std") -> str:
    """Annual cooling demand in kWh of building cb in the bound scenario (NULL for unheated buildings)."""
    setpoint = (f"CASE WHEN CAST(:sp AS numeric) IS NULL OR cb.setpoint_base_c IS NULL THEN 1 "
                f"ELSE {_bound_setpoint(bound)} END")
    return f"(cb.kwh_{bound} * :c_cool * CASE WHEN cb.measures_apply THEN :m_{bound} * {setpoint} ELSE 1 END)"


ANNUAL = demand_expr("std")
ANNUAL_MIN = demand_expr("min")
ANNUAL_MAX = demand_expr("max")
# Required cooling capacity in kW: demand / full-load hours of the usage group (assumption, no oversizing)
CAPACITY = f"({ANNUAL} / NULLIF(cb.vlh_mean, 0))"
AREA = "cb.ngf_m2"
# Electricity if the demand is met by compression chillers: demand / EER of the usage group (assumption).
# The low bound pairs the low demand with the high EER and vice versa.
ELEC = f"({ANNUAL} / NULLIF(cb.eer_mean, 0))"
ELEC_MIN = f"({ANNUAL_MIN} / NULLIF(cb.eer_max, 0))"
ELEC_MAX = f"({ANNUAL_MAX} / NULLIF(cb.eer_min, 0))"
POWER = f"({CAPACITY} / NULLIF(cb.eer_mean, 0))"
# Waste heat a chiller rejects = cooling + its electricity
WASTE = f"({ANNUAL} + {ELEC})"


def total(groups: list, f: dict, bound: str) -> float:
    """Scenario total from sums per (measures_apply, setpoint_base_c) group: the same arithmetic as demand_expr,
    applied to group sums (s_min/s_std/s_max), so any number of scenarios costs one query."""
    out = 0.0
    for g in groups:
        v = float(getattr(g, f"s_{bound}") or 0)
        if g.ma:
            v *= float(f[f"m_{bound}"])
            if f["sp"] is not None and g.sb is not None:
                k = float(f["sp"]) - float(g.sb)
                a = (1 + float(f["sp_pct_min"]) / 100) ** k
                b = (1 + float(f["sp_pct_max"]) / 100) ** k
                v *= {"std": (1 + float(f["sp_pct_std"]) / 100) ** k, "min": min(a, b), "max": max(a, b)}[bound]
        out += v
    return out * float(f["c_cool"])


def scenario(climate=None, year=None, measures=None, setpoint=None) -> dict:
    """The requested scenario with the standard filled in for whatever the question leaves open."""
    sc = dict(STANDARD)
    if climate in CLIMATES:
        sc["climate"] = climate
    if year is not None:
        sc["year"] = max(2025, min(int(year), 2050))
    if measures is not None:
        sc["measures"] = [m for m in measures if m in MEASURES]
    if setpoint in SETPOINTS:
        sc["setpoint"] = float(setpoint)
    return sc


def is_standard(sc: dict) -> bool:
    return (sc["climate"], sc["year"], sorted(sc["measures"]), sc["setpoint"]) == \
        (STANDARD["climate"], STANDARD["year"], sorted(STANDARD["measures"]), STANDARD["setpoint"])


def bind(db: Session, params: dict, sc: dict) -> dict:
    """Add the scenario's factors to the query parameters (for demand_expr / ANNUAL / CAPACITY)."""
    r = db.execute(text("""
        SELECT core_bldg.f_climate_factor(:cl, :yr, 'cool') AS c_cool,
               core_bldg.f_measure_factor(CAST(:ms AS text[]), 'cool', 'min') AS m_min,
               core_bldg.f_measure_factor(CAST(:ms AS text[]), 'cool', 'std') AS m_std,
               core_bldg.f_measure_factor(CAST(:ms AS text[]), 'cool', 'max') AS m_max,
               (SELECT pct_per_k_min FROM core_bldg.cooling_setpoint WHERE setpoint_c = CAST(:sp AS numeric)) AS p_min,
               (SELECT pct_per_k_std FROM core_bldg.cooling_setpoint WHERE setpoint_c = CAST(:sp AS numeric)) AS p_std,
               (SELECT pct_per_k_max FROM core_bldg.cooling_setpoint WHERE setpoint_c = CAST(:sp AS numeric)) AS p_max"""),
                   {"cl": sc["climate"], "yr": sc["year"], "ms": sc["measures"], "sp": sc["setpoint"]}).one()
    params.update({"c_cool": r.c_cool, "m_min": r.m_min, "m_std": r.m_std, "m_max": r.m_max, "sp": sc["setpoint"],
                   "sp_pct_min": r.p_min or 0, "sp_pct_std": r.p_std or 0, "sp_pct_max": r.p_max or 0})
    return params


MEASURE_LABEL = {"verschattung_aussen": "außenliegende Fensterverschattung", "lueftung_nutzer": "Lüftung durch Nutzer",
                 "nachtlueftung_auto": "automatische Nachtlüftung", "fenstertausch": "Fenstertausch",
                 "sanierung": "Sanierung der Hülle"}


def label(sc: dict) -> str:
    """'2030, mittlerer Klimawandel, Sollwert nach Gebäudefunktion, mit außenliegender Fensterverschattung'"""
    parts = [str(sc["year"]), CLIMATES[sc["climate"]],
             f"Sollwert {sc['setpoint']:.0f} °C" if sc["setpoint"] else "Sollwert nach Gebäudefunktion"]
    parts.append("Maßnahmen: " + ", ".join(MEASURE_LABEL[m] for m in sc["measures"]) if sc["measures"]
                 else "ohne Maßnahmen")
    return ", ".join(parts)


def note(sc: dict) -> str:
    kind = "Standardszenario" if is_standard(sc) else "Szenario"
    return (f"Kältebedarf: {kind} {label(sc)}. Annahmen (kWh/(m²·a) je Gebäudefunktion und Baualter mit "
            "Korrekturfaktoren), keine Simulation.")


ACTUAL_NOTE = ("Kälteverbrauch = Schätzung aus der installierten Leistung erkannter Kälteanlagen (Luftbild) mit "
               "Volllaststunden, nur für Gebäude mit erkannter Anlage; Abgleich zum Kältebedarf, große Unsicherheit.")
