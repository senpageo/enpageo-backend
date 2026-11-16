"""Typed tool results ("result frames") -- the data side of the answer, kept apart from its presentation.

A tool returns WHAT was found; choosing HOW to show it (map, bar, line, sunburst, table, text) is a separate,
logged step (the paper's research question). The chooser -- rule heuristic or language model -- only sees
the frame's shape (field types, row count, hierarchy), so the same frame can be judged by every variant.

Field types follow the usual visualization grammar:
  id            building/tree/chiller identifier (joins to map features, e.g. tile feature uuid)
  nominal       unordered category (usage type, district)
  ordinal       ordered category (construction period, month name)
  temporal      time axis (month 1-12)
  quantitative  number, with unit
"""
from .sources import describe

FIELD_TYPES = {"id", "nominal", "ordinal", "temporal", "quantitative"}


def field(name: str, ftype: str, label: str, unit: str | None = None) -> dict:
    assert ftype in FIELD_TYPES, ftype
    f = {"name": name, "type": ftype, "label": label}
    if unit:
        f["unit"] = unit
    return f


def frame(kind: str, fields: list[dict], rows: list[dict], sources: list[str], *, ids: list[str] | None = None,
          hierarchy: list[str] | None = None, total: int | None = None, query: dict | None = None,
          notes: list[str] | None = None) -> dict:
    """kind: 'features' (individual objects), 'aggregate' (groups or one value), 'series' (values over time),
    'none' (the data needed is missing; notes say what).
    hierarchy: grouping fields from outer to inner when there are two or more levels (sunburst-able).
    total: number of matches before truncation, when rows are a sample/top-n."""
    return {
        "kind": kind,
        "fields": fields,
        "rows": rows,
        "row_count": len(rows),
        "total": total if total is not None else len(rows),
        "ids": ids or [],
        "hierarchy": hierarchy or [],
        "sources": describe(sources),
        "query": {k: v for k, v in (query or {}).items() if v is not None},
        "notes": notes or [],
    }


def shape(fr: dict) -> dict:
    """Compact description of a frame for the chooser (no data values, so no variant gets hints from them)."""
    return {
        "kind": fr["kind"],
        "row_count": fr["row_count"],
        "total": fr["total"],
        "has_ids": bool(fr["ids"]),
        "has_geometry": bool(fr["ids"]) or bool(fr.get("geo")),
        "hierarchy_depth": len(fr["hierarchy"]),
        "fields": [{"name": f["name"], "type": f["type"]} for f in fr["fields"]],
    }
