"""Rule baseline of the benchmark: run the tool call of each question and score the chosen view against the expert
ratings. The tool calls are fixed by hand, so this measures the chooser and the tools, not the language model.

    python benchmark/run_rule_baseline.py [ratings.json] [api_url]

ratings.json is written by auswertung_ratings.py (one entry per question: nr, p1, einig, mehrheit, kombi).
"""
import json
import os
import sys
import time

import httpx

RATINGS = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Downloads/Fragebewertung_Auswertung.json")
API = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8001"

LABEL = {"map": "Karte", "bar": "Balkendiagramm", "line": "Liniendiagramm", "sunburst": "Sunburst / Anteile",
         "table": "Tabelle", "kpi": "Einzelne Zahl", "text": "Kurzer Text"}

# "markiertes Gebiet": a block in Neukölln around Leinestraße
AREA = {"type": "Polygon", "coordinates": [[[13.420, 52.468], [13.435, 52.468], [13.435, 52.478], [13.420, 52.478], [13.420, 52.468]]]}
BERLIN = {"scope": "berlin"}

# question number -> (tool, args, uses the marked area)
CALLS = {
    "2 | 22 | 63": ("density_grid", {"metric": "cooling_capacity", **BERLIN}, False),
    "3": ("density_grid", {"metric": "heat_cooling_overlap", **BERLIN}, False),
    "6": ("aggregate_buildings", {"metric": "cooling_specific", "group_by": ["usage_type"], **BERLIN}, False),
    "12 | 59": ("aggregate_buildings", {"metric": "cooling_demand", "group_by": ["postcode"], "top_n": 10, **BERLIN}, False),
    "19": ("find_buildings", {"limit": 200}, True),
    "23 | 68": ("find_buildings", {"usage_zone_type": "sensibel", "districts": ["Neukölln"]}, False),
    "24": ("density_grid", {"metric": "cooling_demand", "cell_m": 100}, True),
    "26": ("find_buildings", {"has_chiller": False, "min_cooling_capacity_kw": 50}, True),
    "36 | 37": ("aggregate_buildings", {"metric": "cooling_specific", "group_by": ["usage_type"]}, True),
    "41": ("aggregate_buildings", {"metric": "building_count", "max_year_built": 1949}, True),
    "42 | 56": ("find_buildings", {"limit": 500}, True),
    "47 | 18 | 13": ("find_chillers", {"limit": 500, **BERLIN}, False),
    "55": ("monthly_profile", {}, True),
    "60": ("find_buildings", {"min_year_built": 1870, "max_year_built": 1918}, True),
    "61": ("aggregate_chillers", {"chiller_group": "cooling_tower", "count": "buildings", "districts": ["Mitte"]}, False),
    "62": ("find_buildings", {"min_cooling_capacity_kw": 2000, "districts": ["Spandau"], "sort_by": "cooling_capacity"}, False),
    "66": ("find_buildings", {"min_cooling_capacity_kw": 1000, "near_place": "Tiergarten", "max_distance_m": 3000, "sort_by": "cooling_capacity"}, False),
    "16 | 17 | 64": ("find_buildings", {"near_place": "Charité Campus Mitte", "max_distance_m": 100, "usage_zone_type": "Krankenhaus"}, False),
}
# needs data that is missing: the answer has to say so, not choose a chart
GAPS = {"1 | 51": "indoor_temperature", "4": "measures", "5": "indoor_temperature", "7": "chiller_capacity",
        "8": "outdoor_heat", "9": "chiller_capacity", "10": "measures", "11": "measures", "14": "ownership",
        "15": "cooling_networks", "20": "chiller_capacity", "21": "cooling_networks", "25": "climate_scenarios",
        "27": "measures", "28": "measures", "29 | 44": "electricity", "30": "pv", "32": "measures", "33 | 54": "electricity",
        "34": "electricity", "35": "electricity", "38": "electricity", "39": "electricity", "40 | 69": "climate_scenarios",
        "43": "measures", "45": "measures", "46": "other_city", "49": "other_city", "50": "outdoor_heat", "52": "electricity",
        "57": "building_physics", "58": "pv", "65": "climate_scenarios", "67": "data_centers"}

fragen = {q["nr"]: q for q in json.load(open(RATINGS, encoding="utf-8"))["fragen"]}
hit_main = hit_any = hit_map = scored = 0
with httpx.Client(timeout=300) as client:
    for nr, (tool, args, marked) in CALLS.items():
        t0 = time.time()
        r = client.post(f"{API}/api/answer", json={"tool": tool, "args": args, "polygon": AREA if marked else None})
        dt = time.time() - t0
        if r.status_code != 200:
            print(f"{nr:14} FEHLER {r.status_code} {r.text[:200]}")
            continue
        d = r.json()
        view, fr = d["view"], d["frame"]
        chosen = [LABEL[view["type"]]] + [LABEL[c] for c in view.get("companions", [])]
        q = fragen[nr]
        gt = q["mehrheit"]
        mark = "-"
        if gt:
            scored += 1
            hit_main += chosen[0] == gt
            hit_any += gt in chosen
            hit_map += gt == "Karte"
            mark = "OK" if chosen[0] == gt else ("teils" if gt in chosen else "nein")
        print(f"{nr:14} {tool:20} {fr['total']:>7} Treffer {dt:5.1f}s  gewählt: {' + '.join(chosen):32} Mehrheit: {str(gt):20} {mark}")
    for nr, topic in GAPS.items():
        r = client.post(f"{API}/api/answer", json={"tool": "report_missing_data", "args": {"topic": topic}})
        assert r.status_code == 200 and r.json()["view"]["type"] == "text" and r.json()["frame"]["notes"], (nr, r.text[:200])
print(f"\nAls Hauptdarstellung richtig: {hit_main} von {scored} | Mehrheit unter den angebotenen Darstellungen: {hit_any} von {scored}"
      f" | Vergleich immer Karte: {hit_map} von {scored}")
print(f"Daten fehlen, Antwort sagt es: {len(GAPS)} Fragen geprüft. Nicht abgedeckt: {sorted(set(fragen) - set(CALLS) - set(GAPS))}")
