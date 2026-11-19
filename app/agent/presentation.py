"""Choosing how an answer is shown: map, bar, line, sunburst, table or text (the paper's research question).

This module holds the rule-based chooser, baseline (a) of the benchmark: it looks only at the shape of the
result frame (frames.shape), never at the question. The language-model variants (b: question only, no tools;
c: question + frame shape) will produce the same view spec, so all three can be scored against the same
expert ground truth.

View spec (what the frontend renders):
  type       map | bar | line | sunburst | table | kpi | text  (main representation)
  companions further representations offered next to it. 148 of the 165 expert ratings named two or more, with the
             table the most named overall (98) but rarely first (10), so the table is usually a companion.
  x, y       field names for bar/line (x: category or month, y: value)
  series     optional field splitting bar/line into several series
  hierarchy  grouping fields outer -> inner for sunburst
  value      value field for sunburst
  title      short heading
  chooser    which variant decided ("rule"; "map_table" = fixed comparison condition, always map + table;
             later "llm_no_tools", "llm_tools")
  rationale  one sentence why, logged for the evaluation
"""
from .frames import shape

MAX_BAR_CATEGORIES = 25  # more groups than this are unreadable as bars -> table
MAX_LINE_SERIES = 8


def _fields(fr, *types):
    return [f for f in fr["fields"] if f["type"] in types]


def _title(value_field, group_fields):
    if not group_fields:
        return value_field["label"]
    return f"{value_field['label']} nach " + " und ".join(g["label"] for g in group_fields)


def choose_rule(fr: dict) -> dict:
    s = shape(fr)
    quantitative = _fields(fr, "quantitative")
    view = {"chooser": "rule"}

    if s["kind"] == "none":
        return {**view, "type": "text", "companions": [], "title": "Daten fehlen noch", "rationale": "benötigte Daten fehlen"}
    if s["total"] == 0 or not fr["rows"]:
        return {**view, "type": "text", "companions": [], "title": "Keine Treffer", "rationale": "leeres Ergebnis"}

    if s["kind"] == "features":
        # individual buildings/trees: where they are is the point of the answer
        if s["has_geometry"] and s["row_count"] > 1:
            return {**view, "type": "map", "companions": ["table"], "title": f"{s['total']} Treffer",
                    "rationale": "einzelne Objekte mit Lage -> Karte, dazu Tabelle"}
        return {**view, "type": "table", "companions": ["map"] if s["has_geometry"] else [], "title": "Treffer",
                "rationale": "einzelnes Objekt -> Tabelle"}

    value = quantitative[0] if quantitative else None
    if s["kind"] == "series":
        series = _fields(fr, "nominal")
        series_field = series[0]["name"] if series else None
        n_series = len({r.get(series_field) for r in fr["rows"]}) if series_field else 1
        if n_series > MAX_LINE_SERIES:
            return {**view, "type": "table", "companions": [], "title": _title(value, series), "rationale": "zu viele Zeitreihen -> Tabelle"}
        time = _fields(fr, "temporal")[0]["name"] if _fields(fr, "temporal") else "month"
        return {**view, "type": "line", "companions": ["table"], "x": time, "y": value["name"], "series": series_field,
                "title": _title(value, []) + (" im Jahresverlauf" if time == "month" else " über die Jahre"),
                "rationale": "Zeitachse -> Liniendiagramm"}

    # aggregate
    groups = [f for f in fr["fields"] if f["type"] in ("nominal", "ordinal")]
    if s["hierarchy_depth"] >= 2:
        return {**view, "type": "sunburst", "companions": ["table"], "hierarchy": fr["hierarchy"], "value": value["name"],
                "title": _title(value, groups), "rationale": "zwei Gruppierungsebenen -> Sunburst"}
    if s["row_count"] == 1:
        return {**view, "type": "kpi", "companions": [], "value": value["name"], "title": _title(value, groups),
                "rationale": "ein einzelner Wert -> Kennzahl"}
    if s["row_count"] > MAX_BAR_CATEGORIES:
        return {**view, "type": "table", "companions": [], "title": _title(value, groups), "rationale": "viele Gruppen -> Tabelle"}
    return {**view, "type": "bar", "companions": ["table"], "x": groups[0]["name"], "y": value["name"], "title": _title(value, groups),
            "rationale": "Vergleich weniger Gruppen -> Balkendiagramm"}


def choose_map_table(fr: dict) -> dict:
    """Fixed comparison condition of the benchmark: always map + table, whatever the question (the classic
    cadastre answer). Objects with a location are shown on the map with the table beside it; results without
    geometry (aggregates over districts, usage types ...) can only be a table."""
    s = shape(fr)
    view = {"chooser": "map_table"}
    if s["kind"] == "none":
        return {**view, "type": "text", "companions": [], "title": "Daten fehlen noch", "rationale": "benötigte Daten fehlen"}
    if s["total"] == 0 or not fr["rows"]:
        return {**view, "type": "text", "companions": [], "title": "Keine Treffer", "rationale": "leeres Ergebnis"}
    title = f"{s['total']} Treffer" if s["kind"] == "features" else "Ergebnis"
    if s["has_geometry"]:
        return {**view, "type": "map", "companions": ["table"], "title": title, "rationale": "fester Modus: Karte + Tabelle"}
    return {**view, "type": "table", "companions": [], "title": title,
            "rationale": "fester Modus: Karte + Tabelle, ohne Lage nur Tabelle"}


CHOOSERS = {"rule": choose_rule, "map_table": choose_map_table}


def choose(fr: dict | None, chooser: str = "rule") -> dict | None:
    if not fr:
        return None
    return CHOOSERS.get(chooser, choose_rule)(fr)
