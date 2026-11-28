"""Tools the chat agent can call. TOOLS is in Anthropic's tool schema format;
_to_openai_tools() below derives the OpenAI/Ollama function-calling format from the same
source so both providers share one definition."""

import json
import re
from functools import lru_cache

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import cooling
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


# ALKIS building function (Gebäudefunktion, 4-digit GFK code in emc.building.gfk). Finer than the EnergyMap usage
# types: "Schule" there also holds every Kita and care building. Everyday words map to code lists here; anything
# else is matched against the official function names (core_bldg.kww_gfk_mapping.gfk_name).
BUILDING_FUNCTION_ALIASES: dict[str, list[int]] = {
    "schule": [3021, 3022], "schulgebaeude": [3021, 3022], "grundschule": [3021], "oberschule": [3021],
    "gymnasium": [3021], "allgemeinbildende schule": [3021], "berufsschule": [3022], "berufsbildende schule": [3022],
    "kita": [3065], "kitas": [3065], "kindergarten": [3065], "kindergaerten": [3065], "kindertagesstaette": [3065],
    "krippe": [3065], "kinderkrippe": [3065],
    "hochschule": [3023], "universitaet": [3023], "uni": [3023], "fachhochschule": [3023],
    "forschungsinstitut": [3024], "forschung": [2160, 3024],
    "schwimmbad": [3220, 3221, 3241], "schwimmbaeder": [3220, 3221, 3241], "hallenbad": [3221],
    "hallenbaeder": [3221], "bad": [3220, 3221, 3241],
    "krankenhaus": [3050, 3051], "krankenhaeuser": [3050, 3051], "klinik": [3051], "kliniken": [3051],
    "pflegeheim": [3052, 1022], "seniorenheim": [1022], "altenheim": [1022], "pflegeeinrichtung": [3052, 1022],
    "sporthalle": [3211], "turnhalle": [3211], "sportgebaeude": [3210, 3211, 3212],
    "hotel": [2071], "pension": [2071], "jugendherberge": [2072],
    "wohnheim": [1020, 1021, 1022, 1023, 1024, 1025], "studentenwohnheim": [1024],
    "kirche": [3041], "moschee": [3046], "synagoge": [3042], "religioese gebaeude": [3040, 3041, 3042, 3043, 3045, 3046],
    "museum": [3034], "theater": [3032], "oper": [3032], "bibliothek": [3037], "kino": [2092],
    "rathaus": [3012], "verwaltungsgebaeude": [3010], "polizei": [3071], "feuerwehr": [3072], "gericht": [3015],
    "botschaft": [3016], "kaserne": [3073], "gefaengnis": [3075], "justizvollzugsanstalt": [3075],
    "buerogebaeude": [2020], "buero": [2020], "kaufhaus": [2051], "einkaufszentrum": [2052], "laden": [2054],
    "supermarkt": [2054], "restaurant": [2081], "gaststaette": [2081], "kantine": [2083],
    "fabrik": [2111], "produktionsgebaeude": [2110, 2111], "werkstatt": [2120], "lagerhalle": [2143],
    "kuehlhaus": [2141], "tankstelle": [2130], "bahnhof": [3091], "flughafen": [3092],
    "parkhaus": [2461], "garage": [2463], "tiefgarage": [2465], "heizwerk": [2580], "umspannwerk": [2522],
    "gewaechshaus": [2740, 2741, 2742, 3272],
}


_GFK_NAMES: list[tuple[int, str]] = []


def _gfk_names(db: Session) -> list[tuple[int, str]]:
    """(gfk, name) for all codes, read once (188 rows, changes only with 03_gfk_kww_mapping.sql)."""
    if not _GFK_NAMES:
        _GFK_NAMES.extend((r.gfk, r.gfk_name) for r in db.execute(text("SELECT gfk, gfk_name FROM core_bldg.kww_gfk_mapping")))
    return _GFK_NAMES


def resolve_building_function(db: Session, value: str) -> list[int]:
    """Everyday word, ALKIS name (part) or 4-digit code(s) -> list of GFK codes; [] if nothing matches."""
    codes = [int(c) for c in re.findall(r"\b\d{4}\b", value)]
    if codes:
        return codes
    key = _fold_umlauts(value).strip().lower()
    for k in (key, key.rstrip("n"), key.rstrip("s"), key[:-2] if key.endswith("en") else key):
        if k in BUILDING_FUNCTION_ALIASES:
            return BUILDING_FUNCTION_ALIASES[k]
    names = _gfk_names(db)
    return [g for g, n in names if key in _fold_umlauts(n).lower()]


def building_function_condition(db: Session, value: str, params: dict) -> str | None:
    # ALKIS has no function code for data centres; they come from OSM/PeeringDB (core_bldg.data_center_building)
    if "rechenzent" in _fold_umlauts(value).lower() or "data cent" in value.lower():
        return DC_IN  # small id list first, so the planner does not scan all buildings
    codes = resolve_building_function(db, value)
    if not codes:
        return None
    params["bf_codes"] = codes
    return "b.gfk = ANY(:bf_codes)"


# KWW-Technikkatalog (core_bldg.kww_*): Endenergie Heizung + Warmwasser je BGF and process-heat relevance (Tab 56),
# plus the free Berlin extras from tools/geodaten_berlin (ownership of the plot, PV potential/stock, data centres).
KWW_SCENARIO = "2025"
KWW_JOINS = f"""
        LEFT JOIN core_bldg.kww_gfk_mapping kg ON kg.gfk = b.gfk
        LEFT JOIN core_bldg.kww_process_heat kph ON kph.function_label = kg.process_heat_function
        LEFT JOIN core_bldg.kww_heat_demand_building kh ON kh.uuid = b.uuid AND kh.scenario = '{KWW_SCENARIO}'
        LEFT JOIN core_bldg.building_ownership own ON own.uuid = b.uuid
        LEFT JOIN core_bldg.pv_potential_building pvp ON pvp.uuid = b.uuid
        LEFT JOIN core_bldg.pv_installed_building pvi ON pvi.uuid = b.uuid
        LEFT JOIN core_bldg.data_center_building dcb ON dcb.uuid = b.uuid
        LEFT JOIN core_bldg.cooling_estimate_building cact ON cact.uuid = b.uuid
        LEFT JOIN core_bldg.network_connection ncon ON ncon.uuid = b.uuid
        LEFT JOIN core_bldg.envelope_building env ON env.uuid = b.uuid
        LEFT JOIN core_bldg.building_climate bcl ON bcl.uuid = b.uuid"""

# Actual cooling from detected chillers (tools/kaelte/kaelte_ist_schaetzung.py): rough, with a min-max band
COOLING_ACTUAL_NOTE = (
    "Ist-Kälteverbrauch und Kältestrom sind grobe Schätzungen aus den im Luftbild erkannten Kälteanlagen "
    "(Kälteleistung je Anlage aus der KIT-Liste mit Spanne, Überdimensionierung, Volllaststunden je Gebäudefunktion, Leistungszahl je "
    "Anlagentyp). Die Unsicherheit ist groß, deshalb immer die Spanne min–max mit angeben. Anlagen versorgen oft "
    "Nachbargebäude mit; die Zuordnung ist noch nicht validiert.")

CHILLER_CAPACITY_NOTE = ("Kälteleistung je Anlage: Schätzung des KIT aus dem Luftbild (Mittelwert einer "
                         "Monte-Carlo-Simulation, Standardabweichung als Unsicherheit), keine gemessenen Werte.")

DC_IN = "b.uuid = ANY(ARRAY(SELECT uuid FROM core_bldg.data_center_building))"
PV_IN = "b.uuid = ANY(ARRAY(SELECT uuid FROM core_bldg.pv_installed_building))"

OWNER_CLASSES = {
    "land_berlin": "own.uuid IS NOT NULL",
    "bezirk": "own.owner_class = 'Land Berlin – Bezirksverwaltung'",
    "hauptverwaltung": "own.owner_class = 'Land Berlin – Hauptverwaltung'",
    "sondervermoegen": "own.owner_class = 'Land Berlin – Sondervermögen/Liegenschaftsfonds'",
    "nicht_landeseigen": "own.uuid IS NULL",
}
OWNER_EXPR = "COALESCE(own.owner_class, 'nicht landeseigen / unbekannt')"
NEW_BUILDING_FROM_YEAR = 2010  # "Neubau": KWW age class 'ab 2010' (EnEV 2009); data has block-wise age classes
_YEAR_INT = 'CASE WHEN pb."Year of construction" ~ \'^[0-9]+$\' THEN pb."Year of construction"::int END'

EXTRA_FILTER_PROPS = {
    "owner": {
        "type": "string", "enum": list(OWNER_CLASSES),
        "description": (
            "Ownership of the PLOT (not the building) from the Berlin state property register: 'land_berlin' (any "
            "state-owned plot), 'bezirk' (district administrations: schools, daycare, sports, parks/allotments), "
            "'hauptverwaltung' (senate administrations), 'sondervermoegen' (state property funds SILB/SODA, "
            "Liegenschaftsfonds), 'nicht_landeseigen' (not state-owned OR unknown: federal, private, companies, "
            "municipal housing companies are not available per building). Use for 'kommunal', 'landeseigen', "
            "'öffentliche Liegenschaften'."),
    },
    "new_building": {
        "type": "boolean",
        "description": f"true: only new buildings (Neubau = year of construction from {NEW_BUILDING_FROM_YEAR}); "
                       "false: only older ones.",
    },
    "has_pv": {
        "type": "boolean",
        "description": "true: only buildings with a registered PV system (> 30 kWp or on public-sector property; "
                       "small private systems are not available per building); false: only without.",
    },
    "min_pv_potential_kwp": {"type": "number", "description": "Minimum rooftop PV potential in kWp (Berlin solar cadastre)"},
    "data_center": {"type": "boolean", "description": "true: only data centres (from OpenStreetMap and PeeringDB, not complete)"},
}


def extra_conditions(params: dict, owner: str | None = None, new_building: bool | None = None,
                     has_pv: bool | None = None, min_pv_potential_kwp: float | None = None,
                     data_center: bool | None = None, **_ignored) -> list[str]:
    cond = []
    if owner in OWNER_CLASSES:
        cond.append(OWNER_CLASSES[owner])
    if new_building is not None:
        cond.append(f"{_YEAR_INT} {'>=' if new_building else '<'} {NEW_BUILDING_FROM_YEAR}")
    if has_pv is not None:
        cond.append(PV_IN if has_pv else "pvi.uuid IS NULL")
    if min_pv_potential_kwp is not None:
        cond.append("COALESCE(pvp.potential_kwp, 0) >= :min_pv_potential_kwp")
        params["min_pv_potential_kwp"] = min_pv_potential_kwp
    if data_center is not None:
        cond.append(DC_IN if data_center else "dcb.uuid IS NULL")
    return cond


def extra_sources(owner=None, has_pv=None, min_pv_potential_kwp=None, data_center=None, **_) -> list[str]:
    return ((["state_property"] if owner else []) + (["pv_installed"] if has_pv is not None else [])
            + (["pv_potential"] if min_pv_potential_kwp is not None else []) + (["data_centers"] if data_center else []))
# Tab 56 lists only non-residential functions; residential buildings (EZFH/MFH) have no process heat -> 'Nein'.
# Other functions missing from Tab 56 stay NULL (not rated).
PROCESS_HEAT_EXPR = ("COALESCE(kph.process_heat, CASE WHEN kg.residential_split OR kg.kww_type IN ('ezfh', 'mfh') "
                     "THEN 'Nein' END)")


def process_heat_condition(value: bool) -> str:
    """'Ja/Nein' (greenhouses: yes for horticulture, no for agriculture) counts as possible process heat."""
    return "kph.process_heat IN ('Ja', 'Ja/Nein')" if value else "COALESCE(kph.process_heat, 'Nein') = 'Nein'"


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
            params={"q": name, "format": "jsonv2", "limit": 5, "polygon_geojson": 1, "polygon_threshold": 0.0005,
                    "viewbox": "13.08,52.68,13.76,52.33", "bounded": 1},
            headers={"User-Agent": "enpageo-coolingmap/1.0"},
            timeout=10,
        )
        hits = r.json() if r.status_code == 200 else []
    except httpx.HTTPError:
        return None
    # stops and platforms named after a site ("Charité - Campus Mitte" bus stop) are not the site itself
    hits = [h for h in hits if h.get("category") not in ("highway", "railway", "public_transport")]
    areas = [h for h in hits if h.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon")]
    words = name.split()
    if not areas and len(words) > 1:
        # "charité campus mitte" has no area of its own, "charité" does: retry with the last word dropped
        shorter = _resolve_place_cached(" ".join(words[:-1]))
        if shorter and shorter["geometry"].get("type") in ("Polygon", "MultiPolygon"):
            return shorter
    if not hits:
        return None
    # prefer an area (a campus, an airport) over a point or a street of the same name
    hit = areas[0] if areas else hits[0]
    return {"geometry": hit["geojson"], "label": hit.get("display_name", name)}


def resolve_place(name: str) -> dict | None:
    """Named place in Berlin -> GeoJSON geometry (OpenStreetMap Nominatim; only the place name is sent)."""
    return _resolve_place_cached(name.strip().lower())


# Selecting single buildings or a named site, shared by all building tools
SELECTION_PROPS = {
    "address": {"type": "string",
                "description": "A Berlin street address ('Hauptstraße 123', 'Leinestr. 55'): just that building. "
                               "Use whenever the question names an address."},
    "place": {"type": "string",
              "description": "A named site or institution in Berlin ('Charité Campus Mitte', 'Flughafen Tempelhof', "
                             "'Rotes Rathaus'): the buildings on it. Not for districts (use districts)."},
}
_ADDRESS_RE = re.compile(r"^\s*(.*?)[\s,]*(\d+\s*[a-zA-Z]?)?\s*$")


def _address_uuids(db: Session, q: str) -> tuple[list[str], str | None]:
    """Buildings at a street address (emp.emblive street + house number, as the map's address search)."""
    m = _ADDRESS_RE.match(q)
    street = (m.group(1) if m else q).strip().lower()
    street = re.sub(r"str\.?$", "straße", street).replace("strasse", "straße")
    number = (m.group(2) or "").replace(" ", "").lower() if m else ""
    if len(street) < 2:
        return [], None
    rows = db.execute(text("""
        SELECT e.uuid, e.street_name FROM emp.emblive e
        WHERE lower(e.street_name) LIKE :like AND lower(e.street_name) ~ :rx
        ORDER BY e.street_name LIMIT 50"""),
        {"like": f"%{street}%",
         "rx": rf"(^|;\s*){re.escape(street)}" + (rf"\s*{re.escape(number)}([^0-9a-z]|$)" if number else "")}).all()
    return [r.uuid for r in rows], (rows[0].street_name if rows else None)


def selection_condition(db: Session, params: dict, address: str | None = None, place: str | None = None,
                        geom: str = "b.geom") -> tuple[str | None, list[str], str | None]:
    """(SQL condition, notes, error) restricting buildings to an address or a named site."""
    if address:
        uuids, label = _address_uuids(db, address)
        if not uuids:
            return None, [], f"Adresse '{address}' wurde im Gebäudebestand nicht gefunden"
        params["sel_uuids"] = uuids
        return "b.uuid = ANY(:sel_uuids)", [f"Adresse aufgelöst als: {label}" + (f" (+{len(uuids) - 1} weitere)" if len(uuids) > 1 else "")], None
    if place:
        hit = resolve_place(place)
        if not hit:
            return None, [], f"Ort '{place}' wurde in Berlin nicht gefunden"
        params["sel_geojson"] = json.dumps(hit["geometry"])
        g = "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:sel_geojson), 4326), 25833)"
        cond = (f"ST_Intersects({geom}, {g})" if hit["geometry"].get("type") in ("Polygon", "MultiPolygon")
                else f"ST_DWithin({geom}, {g}, 30)")
        return cond, [f"Ort aufgelöst als: {hit['label']}"], None
    return None, [], None


MAX_RESULTS = 500
DEFAULT_RESULTS = 200
MIN_CROWN_RADIUS_M = 0.5

# Cooling demand (mock-up kWh/(m²·a) with scenario factors) and required capacity: see cooling.py
COOLING_ANNUAL_EXPR = cooling.ANNUAL
COOLING_CAPACITY_EXPR = cooling.CAPACITY
# Building supplied by a detected chiller (assignment incl. moving it from car parks etc. to the heated neighbour)
HAS_CHILLER_EXPR = "b.uuid = ANY(ARRAY(SELECT uuid FROM core_bldg.cooling_estimate_building))"
FIND_BUILDINGS_SORT = {
    "cooling_annual": "cooling_annual",
    "cooling_capacity": "cooling_capacity_kw",
    "heating_demand": "heating_demand",
    "kww_heat_demand": "kww_heat_demand",
    "pv_potential": "pv_potential_kwp",
    "pv_installed": "pv_installed_kwp",
    "cooling_actual": "cooling_actual_kwh",
    "electricity_cooling": "electricity_cooling_kwh",
    "year_built": "year_built_int",
    "u_mean": "u_mean",
    "u_wall": "u_wall",
    "u_window": "u_window",
    "pet_14h": "pet_14h",
    "t_air_04h": "t_air_04h",
}
CLIMATE_NOTE = "Wärmebelastung: Klimamodell Berlin 2022 (Umweltatlas) für eine austauscharme sommerliche Strahlungswetterlage, Ist-Zustand; PET = gefühlte Temperatur um 14 Uhr, Lufttemperatur um 4 Uhr = nächtliche Überwärmung, je Block(teil)fläche."
ENVELOPE_NOTE = "U-Werte: typische Werte nach Gebäudetyp und Baualter aus der NaiS-DB (Wohnen TABULA-Typgebäude im Ist-Zustand, Nichtwohnen IWU-NWG-Mittelwerte mit Standardfehler); der tatsächliche Sanierungsstand je Gebäude ist nicht bekannt."

BUILDING_FUNCTION_PROP = {
    "type": "string",
    "description": (
        "Specific building function from the official ALKIS cadastre (finer than usage_zone_type). PREFER this "
        "over usage_zone_type whenever the user names a concrete kind of building: 'Schule' (schools only, no "
        "daycare), 'Kita', 'Hochschule', 'Schwimmbad', 'Hallenbad', 'Krankenhaus', 'Pflegeheim', 'Sporthalle', "
        "'Hotel', 'Kirche', 'Museum', 'Theater', 'Bibliothek', 'Rathaus', 'Polizei', 'Feuerwehr', 'Kaufhaus', "
        "'Einkaufszentrum', 'Fabrik', 'Werkstatt', 'Bahnhof', 'Parkhaus', 'Gewächshaus', or any official German "
        "ALKIS function name or part of it (e.g. 'Seniorenheim', 'Kinderheim'), or 4-digit ALKIS codes like '3021'. "
        "'Rechenzentrum' works too (from OpenStreetMap/PeeringDB, not complete)."
    ),
}
PROCESS_HEAT_PROP = {
    "type": "boolean",
    "description": (
        "true: only buildings whose function has process heat according to the KWW heat-planning catalogue "
        "(Tab 56: e.g. swimming pools, hospitals, production, care homes); false: only those without. This is a "
        "relevance flag per building function, not a measured or computed amount."
    ),
}

FIND_BUILDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "building_function": BUILDING_FUNCTION_PROP,
        "process_heat": PROCESS_HEAT_PROP,
        **EXTRA_FILTER_PROPS,
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
        "min_cooling_capacity_kw": {"type": "number", "description": "Minimum required cooling capacity in kW (1 MW = 1000): cooling demand / full-load hours, assumption"},
        "max_cooling_capacity_kw": {"type": "number", "description": "Maximum required cooling capacity in kW"},
        "has_chiller": {"type": "boolean", "description": "true: only buildings supplied by a cooling unit detected from aerial imagery (with estimated actual cooling and electricity use); false: only buildings without one"},
        "districts": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these Berlin districts (partial names); overrides the map view"},
        "scope": {"type": "string", "enum": ["view", "berlin"], "description": "'view' (default): drawn area or visible map. 'berlin': all of Berlin"},
        "near_place": {"type": "string", "description": "Named place in Berlin (e.g. 'Tiergarten'); use with max_distance_m"},
        "max_distance_m": {"type": "number", "description": "With near_place: maximum distance in meters (3 km = 3000)"},
        "sort_by": {"type": "string", "enum": list(FIND_BUILDINGS_SORT), "description": "What the largest results are sorted by (rankings, 'höchster ...'); default cooling_annual. kww_heat_demand = heat demand after the KWW catalogue; u_mean / u_wall / u_window = worst (highest) typical U-values ('schlechteste U-Werte')"},
        **SELECTION_PROPS,
        "min_pet_14h": {"type": "number", "description": "Minimum heat stress by day (PET at 2 pm, °C) at the building. ALWAYS set it when the question asks for buildings with high/strong heat stress (35) or extreme heat stress (41); without it the results are not selected by heat."},
        "heat_vulnerable": {"type": "boolean", "description": "true: only buildings in areas with very unfavourable heat stress day and night or climatically vulnerable areas (Planungshinweiskarte Stadtklima 2022)"},
        "min_cooling_demand": {"type": "number", "description": "Minimum annual cooling demand in kWh"},
        "max_cooling_demand": {"type": "number", "description": "Maximum annual cooling demand in kWh"},
        **cooling.SCENARIO_PROPS,
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
    "ACC_1_Fan",
    "ACC_2_Fans",
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
                "condenser), 'ACC_1_Fan'/'ACC_2_Fans' (small air-cooled units with one or two fans), 'CT_Base_induced'/'CT_Fan_induced'/'CT_forced'/'CT_forced_difficult' "
                "(cooling tower variants), 'Hybrid_supposed' (suspected hybrid unit). Omit to search "
                "all classes."
            ),
        },
        "min_capacity_kw": {
            "type": "number",
            "description": "Minimum estimated cooling capacity of a unit in kW (KIT list, Monte Carlo mean)",
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
            "generic per-usage-type placeholder (not a simulation result). Each building also carries its "
            "ALKIS building function, the heat demand after the KWW catalogue (gross floor area × KWW value, "
            "heating + hot water, 2025) and whether its function has process heat (KWW). For concrete kinds of "
            "buildings (schools, daycare, pools, hospitals ...) use building_function. If a point or line was drawn "
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
    building_function: str | None = None,
    process_heat: bool | None = None,
    owner: str | None = None,
    new_building: bool | None = None,
    has_pv: bool | None = None,
    min_pv_potential_kwp: float | None = None,
    data_center: bool | None = None,
    climate: str | None = None,
    year: int | None = None,
    measures: list[str] | None = None,
    setpoint: float | None = None,
    address: str | None = None,
    place: str | None = None,
    min_pet_14h: float | None = None,
    heat_vulnerable: bool | None = None,
) -> dict:
    limit = min(int(limit or DEFAULT_RESULTS), MAX_RESULTS)

    conditions = []
    params: dict = {"limit": limit}
    sc = cooling.scenario(climate, year, measures, setpoint)
    cooling.bind(db, params, sc)
    notes: list[str] = []
    sources = ["footprints", "building_params", "energymap_heating", "cooling_mockup"]

    sel, sel_notes, sel_error = selection_condition(db, params, address, place)
    if sel_error:
        return {"error": sel_error}
    notes += sel_notes
    if sel:
        conditions.append(sel)
    elif near_place:
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
    if building_function:
        cond = building_function_condition(db, building_function, params)
        if cond is None:
            return {"error": f"Gebäudefunktion '{building_function}' ist im ALKIS-Katalog nicht bekannt"}
        conditions.append(cond)
        sources.append("data_centers" if cond == DC_IN else "alkis_function")
    if process_heat is not None:
        conditions.append(process_heat_condition(process_heat))
        sources.append("kww_process_heat")
    conditions += extra_conditions(params, owner, new_building, has_pv, min_pv_potential_kwp, data_center)
    sources += extra_sources(owner=owner, has_pv=has_pv, min_pv_potential_kwp=min_pv_potential_kwp,
                             data_center=data_center)

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
            notes.append("„Keine Kälteanlage“ heißt: im Luftbild nichts erkannt. Innenliegende Anlagen sieht die Erkennung nicht.")
    if min_pet_14h is not None:
        conditions.append("bcl.pet14h >= :min_pet_14h")
        params["min_pet_14h"] = min_pet_14h
    if heat_vulnerable is not None:
        conditions.append(("" if heat_vulnerable else "NOT ") + "COALESCE(bcl.very_unfavourable OR bcl.vulnerable_area, false)")
    if min_cooling_demand is not None:
        conditions.append(f"({COOLING_ANNUAL_EXPR}) >= :min_cooling_demand")
        params["min_cooling_demand"] = min_cooling_demand
    if max_cooling_demand is not None:
        conditions.append(f"({COOLING_ANNUAL_EXPR}) <= :max_cooling_demand")
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

    # Heating stays building-specific via emp.emblive (EnergyMap) and the KWW catalogue; cooling demand is the
    # mock-up per building function and age class in the requested scenario (cooling.py).
    base_from = """
        FROM emc.building b
        LEFT JOIN emc.param_building pb ON pb.bldg_uuid = b.uuid
        LEFT JOIN emp.emblive el ON el.uuid = b.uuid
        {COOL_JOIN}
        {KWW_JOINS}
        WHERE 1=1
    """.replace("{KWW_JOINS}", KWW_JOINS).replace("{COOL_JOIN}", cooling.COOL_JOIN)
    sources += ["alkis_function", "kww_heat", "kww_process_heat"]
    sources = list(dict.fromkeys(sources))

    total = db.execute(text(f"SELECT count(*) AS total {base_from} {where_extra}"), params).one().total

    rows = db.execute(
        text(
            f"""
            SELECT
                b.uuid AS bldg_uuid,
                ST_AsGeoJSON(ST_Transform(b.geom, 4326)) AS geom,
                ({COOLING_ANNUAL_EXPR}) AS cooling_annual,
                ({cooling.ANNUAL_MIN}) AS cooling_annual_min,
                ({cooling.ANNUAL_MAX}) AS cooling_annual_max,
                cb.kwh_m2a_std AS cooling_specific_base,
                ({COOLING_CAPACITY_EXPR}) AS cooling_capacity_kw,
                ({cooling.ELEC}) AS electricity_demand_cooling_kwh,
                ({cooling.POWER}) AS electric_power_cooling_kw,
                el.cons_c2r2 AS heating_demand,
                pb."PrimaryUsageZoneType" AS usage_zone_type,
                b.gfk,
                kg.gfk_name AS building_function,
                kh.heat_dhw_kwh_a AS kww_heat_demand,
                {PROCESS_HEAT_EXPR} AS process_heat,
                {OWNER_EXPR} AS plot_owner,
                pvp.potential_kwp AS pv_potential_kwp,
                pvi.installed_kwp AS pv_installed_kwp,
                dcb.names AS data_center,
                cact.cooling_kwh_mean AS cooling_actual_kwh, cact.cooling_kwh_min AS cooling_actual_min,
                cact.cooling_kwh_max AS cooling_actual_max,
                cact.electricity_kwh_mean AS electricity_cooling_kwh, cact.electricity_kwh_min AS electricity_cooling_min,
                cact.electricity_kwh_max AS electricity_cooling_max,
                cact.capacity_kw_mean AS chiller_capacity_kw, cact.check_flags AS chiller_check,
                ncon.length_m AS connection_length_m,
                env.u_wall, env.u_wall_se, env.u_roof, env.u_roof_se, env.u_floor, env.u_floor_se,
                env.u_window, env.u_window_se, env.u_mean, env.source AS envelope_source, env.envelope_type,
                bcl.pet14h AS pet_14h, bcl.t2m04h AS t_air_04h, bcl.rating AS bioclimate,
                bcl.very_unfavourable AS heat_very_unfavourable, bcl.vulnerable_area AS heat_vulnerable_area,
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
                **{k: (round(float(getattr(row, k)), 1) if getattr(row, k) is not None else None)
                   for k in ("cooling_annual", "cooling_annual_min", "cooling_annual_max", "cooling_capacity_kw",
                             "electricity_demand_cooling_kwh", "electric_power_cooling_kw")},
                "heating_demand": row.heating_demand,
                "usage_zone_type": row.usage_zone_type,
                "building_function": row.building_function,
                "gfk": str(row.gfk) if row.gfk is not None else None,  # a code, not a number (no "3.022")
                "year_built": row.year_built,
                "kww_heat_demand": float(row.kww_heat_demand) if row.kww_heat_demand is not None else None,
                "process_heat": row.process_heat,
                "plot_owner": row.plot_owner,
                "pv_potential_kwp": float(row.pv_potential_kwp) if row.pv_potential_kwp is not None else None,
                "pv_installed_kwp": float(row.pv_installed_kwp) if row.pv_installed_kwp is not None else None,
                "data_center": row.data_center,
                **{k: (float(getattr(row, k)) if getattr(row, k) is not None else None)
                   for k in ("cooling_actual_kwh", "cooling_actual_min", "cooling_actual_max", "electricity_cooling_kwh",
                             "electricity_cooling_min", "electricity_cooling_max", "chiller_capacity_kw",
                             "connection_length_m", "u_wall", "u_wall_se", "u_roof", "u_roof_se", "u_floor",
                             "u_floor_se", "u_window", "u_window_se", "u_mean", "pet_14h", "t_air_04h")},
                "bioclimate": row.bioclimate,
                "heat_very_unfavourable": row.heat_very_unfavourable,
                "heat_vulnerable_area": row.heat_vulnerable_area,
                "envelope_source": row.envelope_source,
                "chiller_check": row.chiller_check or None,
            },
        }
        for row in rows
    ]

    if any(f["properties"]["cooling_actual_kwh"] is not None for f in features):
        sources += ["chillers", "chiller_estimate"]
        sources = list(dict.fromkeys(sources))
    cooling_values = [f["properties"]["cooling_annual"] for f in features if f["properties"]["cooling_annual"] is not None]
    heating_values = [f["properties"]["heating_demand"] for f in features if f["properties"]["heating_demand"] is not None]
    kww_values = [f["properties"]["kww_heat_demand"] for f in features if f["properties"]["kww_heat_demand"] is not None]
    functions: dict[str, int] = {}
    for f in features:
        bf = f["properties"]["building_function"] or "unbekannt"
        functions[bf] = functions.get(bf, 0) + 1

    # Typed result for the presentation chooser (frames.py); geometry stays out of it, the map joins by uuid.
    building_frame = frame(
        "features",
        [
            field("bldg_uuid", "id", "Gebäude-ID"),
            field("usage_zone_type", "nominal", "Nutzungsart"),
            field("building_function", "nominal", "Gebäudefunktion (ALKIS)"),
            field("gfk", "nominal", "ALKIS-Code"),
            field("year_built", "ordinal", "Baujahr"),
            field("heating_demand", "quantitative", "Heizwärmebedarf", "kWh/a"),
            field("kww_heat_demand", "quantitative", "Wärmebedarf KWW (Heizung + Warmwasser)", "kWh/a"),
            field("process_heat", "nominal", "Prozesswärme (KWW)"),
            field("plot_owner", "nominal", "Grundstückseigentum"),
            field("pv_potential_kwp", "quantitative", "PV-Potenzial Dach", "kWp"),
            field("pv_installed_kwp", "quantitative", "PV installiert (ab 30 kWp / öffentlich)", "kWp"),
            field("data_center", "nominal", "Rechenzentrum"),
            field("cooling_annual", "quantitative", "Kältebedarf", "kWh/a"),
            field("cooling_annual_min", "quantitative", "Kältebedarf min", "kWh/a"),
            field("cooling_annual_max", "quantitative", "Kältebedarf max", "kWh/a"),
            field("cooling_capacity_kw", "quantitative", "benötigte Kälteleistung", "kW"),
            field("electricity_demand_cooling_kwh", "quantitative", "Kältestrom bei Deckung des Bedarfs", "kWh/a"),
            field("electric_power_cooling_kw", "quantitative", "elektrische Leistung der Kälteerzeugung", "kW"),
            field("cooling_actual_kwh", "quantitative", "Kälteverbrauch (aus installierter Leistung, Abgleich)", "kWh/a"),
            field("cooling_actual_min", "quantitative", "Kälteverbrauch min", "kWh/a"),
            field("cooling_actual_max", "quantitative", "Kälteverbrauch max", "kWh/a"),
            field("chiller_capacity_kw", "quantitative", "installierte Kälteleistung erkannter Anlagen", "kW"),
            field("electricity_cooling_kwh", "quantitative", "Kältestrom (Schätzung, Mittel)", "kWh/a"),
            field("electricity_cooling_min", "quantitative", "Kältestrom min", "kWh/a"),
            field("electricity_cooling_max", "quantitative", "Kältestrom max", "kWh/a"),
            field("chiller_check", "nominal", "Zuordnung prüfen"),
            field("connection_length_m", "quantitative", "Anschluss an Straße", "m"),
            field("u_mean", "quantitative", "mittlerer U-Wert (typisch)", "W/(m²K)"),
            field("u_wall", "quantitative", "U-Wert Wand", "W/(m²K)"),
            field("u_wall_se", "quantitative", "U-Wert Wand ±", "W/(m²K)"),
            field("u_roof", "quantitative", "U-Wert Dach", "W/(m²K)"),
            field("u_roof_se", "quantitative", "U-Wert Dach ±", "W/(m²K)"),
            field("u_floor", "quantitative", "U-Wert Boden", "W/(m²K)"),
            field("u_floor_se", "quantitative", "U-Wert Boden ±", "W/(m²K)"),
            field("u_window", "quantitative", "U-Wert Fenster", "W/(m²K)"),
            field("u_window_se", "quantitative", "U-Wert Fenster ±", "W/(m²K)"),
            field("envelope_source", "nominal", "Quelle U-Werte"),
            field("pet_14h", "quantitative", "Wärmebelastung am Tag (PET 14 Uhr)", "°C"),
            field("t_air_04h", "quantitative", "Lufttemperatur nachts (4 Uhr)", "°C"),
            field("bioclimate", "nominal", "Bioklimatische Bewertung"),
            field("heat_very_unfavourable", "nominal", "sehr ungünstig belastetes Gebiet"),
            field("heat_vulnerable_area", "nominal", "stadtklimatisch vulnerables Gebiet"),
        ],
        [
            {k: (round(float(v), 1) if k in ("heating_demand", "kww_heat_demand") and v is not None else v)
             for k, v in f["properties"].items()}
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
               "max_distance_m": max_distance_m, "sort_by": sort_by, "building_function": building_function,
               "process_heat": process_heat, "owner": owner, "new_building": new_building, "has_pv": has_pv,
               "min_pv_potential_kwp": min_pv_potential_kwp, "data_center": data_center, **sc,
               "min_pet_14h": min_pet_14h, "heat_vulnerable": heat_vulnerable, "address": address, "place": place},
        notes=notes + [cooling.note(sc), "Benötigte Kälteleistung = Kältebedarf / Volllaststunden der Nutzung (Annahme).",
                       ENVELOPE_NOTE, CLIMATE_NOTE]
              + ([cooling.ACTUAL_NOTE] if any(f["properties"]["cooling_actual_kwh"] for f in features) else []),
    )

    return {
        "layer": "buildings",
        "geojson": {"type": "FeatureCollection", "features": features},
        "frame": building_frame,
        "summary_for_llm": {
            "total_matches": total,
            "returned": len(features),
            "truncated": total > len(features),
            "cooling_scenario": cooling.label(sc),
            "heat_filter": (f"min_pet_14h = {min_pet_14h} °C" if min_pet_14h is not None else
                            "KEIN Hitze-Filter: die Treffer sind nicht nach Wärmebelastung ausgewählt"),
            "pet_14h_of_returned": {
                "mean": round(sum(p) / len(p), 1) if (p := [f["properties"]["pet_14h"] for f in features if f["properties"]["pet_14h"] is not None]) else None,
                "count_over_35": sum(1 for v in p if v > 35), "count_over_41": sum(1 for v in p if v > 41),
                "returned_with_value": len(p)},
            "heat_stress_top": [{"building_function": f["properties"]["building_function"],
                                 "pet_14h": f["properties"]["pet_14h"], "t_air_04h": f["properties"]["t_air_04h"],
                                 "bioclimate": f["properties"]["bioclimate"]} for f in features[:5]]
                               if sort_by in ("pet_14h", "t_air_04h") or min_pet_14h is not None else None,
            "standard_scenario": cooling.is_standard(sc),
            "sum_cooling_demand_kwh": round(sum(cooling_values)) if cooling_values else None,
            "avg_cooling_demand_kwh": round(sum(cooling_values) / len(cooling_values), 1) if cooling_values else None,
            "avg_heating_demand_kwh": round(sum(heating_values) / len(heating_values), 1) if heating_values else None,
            "sum_kww_heat_demand_kwh": round(sum(kww_values)) if kww_values else None,
            "building_functions": dict(sorted(functions.items(), key=lambda kv: -kv[1])[:8]),
            "with_process_heat": sum(1 for f in features if f["properties"]["process_heat"] in ("Ja", "Ja/Nein")),
            "on_state_owned_plot": sum(1 for f in features if f["properties"]["plot_owner"].startswith("Land Berlin")),
            "sum_pv_potential_kwp": round(sum(f["properties"]["pv_potential_kwp"] or 0 for f in features)),
            "with_pv_installed": sum(1 for f in features if f["properties"]["pv_installed_kwp"]),
            "data_centers": sorted({f["properties"]["data_center"] for f in features if f["properties"]["data_center"]})[:10],
            "with_detected_chiller": sum(1 for f in features if f["properties"]["cooling_actual_kwh"] is not None),
            "sum_cooling_actual_kwh": {k: round(sum(f["properties"][c] or 0 for f in features)) for k, c in
                                       (("min", "cooling_actual_min"), ("mean", "cooling_actual_kwh"), ("max", "cooling_actual_max"))},
            "sum_electricity_cooling_kwh": {k: round(sum(f["properties"][c] or 0 for f in features)) for k, c in
                                            (("min", "electricity_cooling_min"), ("mean", "electricity_cooling_kwh"),
                                             ("max", "electricity_cooling_max"))},
            "note_actual_cooling": COOLING_ACTUAL_NOTE,
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
    min_confidence: float | None = None,  # not in the 2026 list any more; ignored
    min_capacity_kw: float | None = None,
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
        conditions.append(district_condition(districts, params, 'core_bldg."Chillers".geom'))
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
    if min_capacity_kw is not None:
        conditions.append("capacity_kw >= :min_capacity_kw")
        params["min_capacity_kw"] = min_capacity_kw
    if min_shape_area is not None:
        conditions.append("shape_area >= :min_shape_area")
        params["min_shape_area"] = min_shape_area
    if max_shape_area is not None:
        conditions.append("shape_area <= :max_shape_area")
        params["max_shape_area"] = max_shape_area

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = db.execute(
        text(f'SELECT count(*) AS total FROM core_bldg."Chillers" {where_clause}'), params
    ).one().total

    rows = db.execute(
        text(
            f"""
            SELECT objectid, class AS class_name, fans, capacity_kw, capacity_sd_kw, shape_area,
                   (SELECT r.placement FROM core_bldg.chiller_3d r
                     WHERE r.objectid = core_bldg."Chillers".objectid) AS placement,
                   -- outline with roof heights (tools/kaelte/anlagen_3d.py): drawn as a block, like the map layer
                   ST_AsGeoJSON(COALESCE((SELECT r.geom FROM core_bldg.chiller_3d r
                                           WHERE r.objectid = core_bldg."Chillers".objectid),
                                         ST_Force3D(ST_Transform(geom, 4326))), 7) AS geom
            FROM core_bldg."Chillers"
            {where_clause}
            ORDER BY capacity_kw DESC NULLS LAST, shape_area DESC NULLS LAST
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
                "fans": row.fans,
                "capacity_kw": row.capacity_kw,
                "capacity_sd_kw": row.capacity_sd_kw,
                "shape_area": row.shape_area,
                "placement": row.placement,
            },
        }
        for row in rows
    ]

    chiller_frame = frame(
        "features",
        [
            field("objectid", "id", "Objekt-ID"),
            field("class", "nominal", "Klasse"),
            field("fans", "quantitative", "Lüfter", "Stück"),
            field("capacity_kw", "quantitative", "Kälteleistung (KIT-Schätzung)", "kW"),
            field("capacity_sd_kw", "quantitative", "Kälteleistung – Standardabweichung", "kW"),
            field("shape_area", "quantitative", "Grundfläche", "m²"),
        ],
        [{k: (round(float(v), 1) if k in ("capacity_kw", "capacity_sd_kw", "shape_area") and v is not None else v)
          for k, v in f["properties"].items() if k != "placement"} for f in features],
        ["chillers"] + (["districts"] if districts else []),
        ids=[str(f["properties"]["objectid"]) for f in features],
        total=total,
        query={"chiller_class": chiller_class, "districts": districts, "scope": scope},
        notes=[CHILLER_CAPACITY_NOTE],
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
