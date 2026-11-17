"""English output for the chat answers (the map's /en pages).

The tools build their result frames in German (labels, units, notes, source descriptions, category values).
Rather than threading a language through every tool, localize() translates a finished frame once, at the
API boundary. Unknown strings stay German, so a new tool never breaks, it is only not translated yet.
Usage types mirror website src/lib/cesiumMap/usage.ts.
"""
import copy
import re

EXACT = {
    # field labels
    "Nutzungsart": "Usage type",
    "Bezirk": "District",
    "Baualtersklasse": "Construction period",
    "Postleitzahl": "Postcode",
    "Heizwärmebedarf": "Heating demand",
    "spez. Heizwärmebedarf": "Specific heating demand",
    "Kältebedarf (Platzhalter)": "Cooling demand (placeholder)",
    "spez. Kältebedarf (Platzhalter)": "Specific cooling demand (placeholder)",
    "Kälteleistung (Platzhalter)": "Cooling capacity (placeholder)",
    "beheizte Fläche": "Heated area",
    "Anzahl Gebäude": "Number of buildings",
    "Monat": "Month",
    "Rang": "Rank",
    "Länge": "Longitude",
    "Breite": "Latitude",
    "Gebäude mit Kühlanlage": "Buildings with a chiller",
    "Erkannte Kühlanlagen": "Detected chillers",
    "Klasse": "Class",
    "Gebäude-ID": "Building ID",
    "Baujahr": "Year of construction",
    "Objekt-ID": "Object ID",
    "Erkennungssicherheit": "Detection confidence",
    "Grundfläche": "Footprint area",
    "Gebäudefunktion (ALKIS)": "Building function (ALKIS)",
    "ALKIS-Code": "ALKIS code",
    "Wärmebedarf KWW (Heizung + Warmwasser)": "Heat demand KWW (heating + hot water)",
    "Prozesswärme (KWW)": "Process heat (KWW)",
    "Ja": "Yes", "Nein": "No", "Ja/Nein": "Yes/No",
    "ALKIS-Gebäudefunktion (GFK)": "ALKIS building function (GFK)",
    "Wärmebedarf nach KWW-Technikkatalog Wärmeplanung (08-2026, V1.1), Stand 2025":
        "Heat demand after the KWW heat-planning technology catalogue (08-2026, v1.1), 2025",
    "Prozesswärme-Relevanz nach KWW-Technikkatalog (Tab 56)": "Process-heat relevance after the KWW catalogue (Tab 56)",
    "Bruttogeschossfläche": "Gross floor area",
    "Kennwertverfahren, bundesweite Mittelwerte": "Benchmark method, national averages",
    "Grundstückseigentum": "Plot ownership",
    "PV-Potenzial Dach": "Rooftop PV potential",
    "PV installiert (ab 30 kWp / öffentlich)": "PV installed (from 30 kWp / public sector)",
    "Rechenzentrum": "Data centre",
    "Land Berlin – Bezirksverwaltung": "State of Berlin – district administration",
    "Land Berlin – Hauptverwaltung": "State of Berlin – senate administration",
    "Land Berlin – Sondervermögen/Liegenschaftsfonds": "State of Berlin – property funds",
    "nicht landeseigen / unbekannt": "not state-owned / unknown",
    "Berliner Landesgrundvermögen (Grundstücke des Landes Berlin)": "Berlin state property register (plots owned by the State of Berlin)",
    "Solarpotenzial Berlin (Solarrechner), geeignete Dachflächen je Gebäude": "Berlin solar cadastre, suitable roof areas per building",
    "PV-Bestandsanlagen, Umweltatlas Berlin (Basis Marktstammdatenregister)": "Existing PV systems, Berlin environmental atlas (based on the market master data register)",
    "Rechenzentren aus OpenStreetMap und PeeringDB": "Data centres from OpenStreetMap and PeeringDB",
    "unvollständig": "incomplete",
    "kleine private Anlagen fehlen je Gebäude": "small private systems missing per building",
    # units
    "Gebäude": "buildings",
    "Anlagen": "units",
    "kWh/Monat": "kWh/month",
    # category values
    "unbekannt": "unknown",
    "bis 1918": "until 1918",
    "ab 2010": "from 2010",
    "Mär": "Mar", "Mai": "May", "Okt": "Oct", "Dez": "Dec",
    # notes
    "Keine Gebäude in diesem Gebiet im Datenbestand. Vorhanden ist derzeit nur Berlin.":
        "No buildings in this area in the dataset. Currently only Berlin is available.",
    "Kältewerte sind generische Platzhalter je Nutzungsart, keine Simulation.":
        "Cooling values are generic placeholders per usage type, not a simulation.",
    "Kältebedarf ist ein generischer Platzhalter; die Monatsverteilung ist für alle Nutzungsarten gleich (Mai–September).":
        "Cooling demand is a generic placeholder; the monthly distribution is the same for all usage types (May–September).",
    "Aus Luftbildern erkannt. Die Kälteleistung je Anlage liegt noch nicht vor (kommt vom KIT).":
        "Detected from aerial images. The cooling capacity per unit is not available yet (to come from KIT).",
    "Die Kälteleistung je Anlage liegt noch nicht vor (kommt vom KIT).":
        "The cooling capacity per unit is not available yet (to come from KIT).",
    # sources
    "EnergyMap Berlin, Heizwärmebedarf je Gebäude": "EnergyMap Berlin, heating demand per building",
    "2026-06-25, kalibrierte Simulation": "2026-06-25, calibrated simulation",
    "Gebäudesimulation, kalibriert; Variante c2r2": "Building simulation, calibrated; variant c2r2",
    "Nettoraumfläche (spec = cons / nrf_area)": "Net room area (spec = cons / nrf_area)",
    "Kältebedarf, generischer Platzhalter je Nutzungsart": "Cooling demand, generic placeholder per usage type",
    "spezifischer Jahreskältebedarf je Nutzungsart × Nutzfläche; Monatsverteilung für alle Nutzungsarten gleich "
    "(Mai 10 %, Jun 20 %, Jul 30 %, Aug 30 %, Sep 10 %). Keine Simulation.":
        "Specific annual cooling demand per usage type × usable area; monthly distribution the same for all usage "
        "types (May 10 %, Jun 20 %, Jul 30 %, Aug 30 %, Sep 10 %). Not a simulation.",
    "grobe Abschätzung": "rough estimate",
    "PrimaryUsageZoneArea, sonst Heated area": "PrimaryUsageZoneArea, otherwise heated area",
    "Gebäudeparameter (Nutzung, Baujahr, Flächen)": "Building parameters (usage, year of construction, areas)",
    "Gebäudegrundrisse": "Building footprints",
    "3D-Gebäudemodell LoD2 Berlin": "3D building model LoD2 Berlin",
    "Zuordnung 2D↔3D: Verknüpfungstabelle, räumlicher Rückfall, sonst extrudierter Grundriss (als geschätzt markiert)":
        "2D↔3D matching: link table, spatial fallback, otherwise extruded footprint (marked as estimated)",
    "Baumbestand Berlin (Straßen- und Anlagenbäume)": "Berlin tree register (street and park trees)",
    "Rückkühler auf Dächern, aus Luftbildern erkannt": "Rooftop chillers, detected from aerial images",
    "Objekterkennung in Luftbildern, mit Konfidenz je Objekt": "Object detection in aerial images, with confidence per object",
    "Ortsnamen aus OpenStreetMap (Nominatim)": "Place names from OpenStreetMap (Nominatim)",
    "Ortsname wird in Berlin nachgeschlagen; übertragen wird nur der Name":
        "The place name is looked up in Berlin; only the name is transmitted",
    "Bezirksgrenzen Berlin": "Berlin district boundaries",
    # missing data (report_missing_data: "Fehlt noch: <what>. <Why>.")
    "Klimaszenarien und Zukunftsjahre": "Climate scenarios and future years",
    "Wirkungen von Maßnahmen und Sanierungen auf den Kältebedarf": "Effects of measures and retrofits on cooling demand",
    "Innenraumtemperaturen und Überhitzungsstunden": "Indoor temperatures and overheating hours",
    "Angaben zur Wärmebelastung im Außenraum": "Outdoor heat stress",
    "Strombedarf und Lastgänge": "Electricity demand and load profiles",
    "Angaben zum PV-Potenzial": "PV potential",
    "Angaben zu Kältenetzen": "Cooling networks",
    "Kälteleistungen je erkannter Anlage": "Cooling capacity per detected unit",
    "Gebäudedaten außerhalb Berlins": "Building data outside Berlin",
    "Angaben zum Eigentum (z. B. kommunale Liegenschaften)": "Ownership (e.g. municipal properties)",
    "U-Werte und Sanierungsstand je Gebäude": "U-values and retrofit status per building",
    "Rechenzentren als eigene Nutzungsart": "Data centres as a separate usage type",
    "Diese Daten": "These data",
    "Kommen aus der Simulation im Projekt": "Will come from the simulation in the project",
    "Die Einbindung der HOSTRADA-Klimadaten ist geplant": "Integrating the HOSTRADA climate data is planned",
    "Sind noch nicht im Datenbestand": "Are not in the dataset yet",
    "Standorte und Typen sind da, die Leistung kommt vom KIT": "Locations and types are available, the capacity will come from KIT",
    "Stuttgart und Korbach folgen im Projekt, weitere Städte sind nicht vorgesehen":
        "Stuttgart and Korbach will follow in the project, other cities are not planned",
}

USAGE = {
    "Einfamilien-/ Reihenhaus": "Single-family / terraced house",
    "Unbeheizt": "Unheated",
    "(Grosses) Mehrfamilienhaus": "(Large) multi-family house",
    "Produktions-, Werkstatt-, Lager- oder Betriebsgebaeude": "Production, workshop, storage or operations building",
    "Handelsgebaeude": "Retail building",
    "Freizeit mit Tagesnutzung": "Leisure, daytime use",
    "Buero-, Verwaltungs- oder Amtsgebaeude": "Office or administrative building",
    "Schule, Kindertagesstaette und sonstige Betreuungsgebaeude": "School, day care or other care building",
    "Beherberungs- oder Unterbringungsgebaeude": "Accommodation building",
    "Sporthalle": "Sports hall",
    "Technikgebaeude (Ver- und Entsorgung)": "Technical building (utilities)",
    "Gebaeude fuer Forschung und Hochschullehre": "Research and university building",
    "Produktions- und Technikgebaeude": "Production and technical building",
    "Gastronomie- oder Verpflegungsgebaeude": "Restaurant or catering building",
    "Pflegeheim": "Care home",
    "Verkehrsgebaeude": "Transport building",
    "Krankenhaus": "Hospital",
    "Polizei": "Police",
    "Arztpraxen": "Medical practices",
    "Feuerwehr": "Fire station",
    "Freizeit mit Abendnutzung": "Leisure, evening use",
    "Schwimmbad": "Swimming pool",
}

PATTERNS = [
    (re.compile(r"^Anteil an (.+) gesamt$"), lambda m: f"Share of total {tr(m.group(1)).lower()}"),
    (re.compile(r"^Rasterzellen (\d+) m × (\d+) m, gezeigt werden die dichtesten (\d+)\. Kältewerte sind generische "
                r"Platzhalter, keine Simulation\. Eine echte Liniendichte braucht das Straßennetz und fehlt noch\.$"),
     lambda m: f"Grid cells {m.group(1)} m × {m.group(2)} m, showing the densest {m.group(3)}. Cooling values are "
               "generic placeholders, not a simulation. A real linear density needs the street network, which is "
               "still missing."),
    (re.compile(r"^Fehlt noch: (.+)\.$"), lambda m: _missing(m.group(1))),
    (re.compile(r"^Gebäudedaten für (.+)$"), lambda m: f"Building data for {m.group(1)}"),
]


def _missing(body: str) -> str:
    """"<what>. <Why>" -- <what> may itself contain ". " (z. B.), so split where the left part is known."""
    parts = body.split(". ")
    for k in range(len(parts) - 1, 0, -1):
        what, why = ". ".join(parts[:k]), ". ".join(parts[k:])
        if tr(what) != what or what.startswith("Gebäudedaten für "):
            return f"Not available yet: {tr(what)}. {tr(why)}."
    return f"Not available yet: {body}."


def tr(s):
    if not isinstance(s, str):
        return s
    if s in EXACT:
        return EXACT[s]
    if s in USAGE:
        return USAGE[s]
    for rx, fn in PATTERNS:
        m = rx.match(s)
        if m:
            return fn(m)
    return s


# row fields that hold German category values (not names, numbers or ids)
_VALUE_FIELDS = {"usage_type", "usage_zone_type", "construction_period", "month_label", "process_heat", "plot_owner"}


def localize(fr: dict | None, lang: str | None) -> dict | None:
    """Frame in the requested language; German (the tools' own language) is returned unchanged."""
    if not fr or (lang or "de").lower().startswith("de"):
        return fr
    fr = copy.deepcopy(fr)
    for f in fr.get("fields", []):
        f["label"] = tr(f.get("label"))
        if "unit" in f:
            f["unit"] = tr(f["unit"])
    for row in fr.get("rows", []):
        for k in _VALUE_FIELDS & row.keys():
            row[k] = tr(row[k])
    fr["notes"] = [tr(n) for n in fr.get("notes", [])]
    for src in fr.get("sources", []):
        for k in ("label", "status", "method", "accuracy", "reference_area"):
            if src.get(k):
                src[k] = tr(src[k])
    return fr


VIEW_EXACT = {
    "Daten fehlen noch": "Data not available yet",
    "Keine Treffer": "No results",
    "Treffer": "Result",
    "benötigte Daten fehlen": "required data missing",
    "leeres Ergebnis": "empty result",
    "einzelne Objekte mit Lage -> Karte, dazu Tabelle": "individual objects with location -> map, plus table",
    "einzelnes Objekt -> Tabelle": "single object -> table",
    "zu viele Zeitreihen -> Tabelle": "too many time series -> table",
    "Zeitachse -> Liniendiagramm": "time axis -> line chart",
    "zwei Gruppierungsebenen -> Sunburst": "two grouping levels -> sunburst",
    "ein einzelner Wert -> Kennzahl": "a single value -> key figure",
    "viele Gruppen -> Tabelle": "many groups -> table",
    "Vergleich weniger Gruppen -> Balkendiagramm": "comparison of a few groups -> bar chart",
    "fester Modus: Karte + Tabelle": "fixed mode: map + table",
    "fester Modus: Karte + Tabelle, ohne Lage nur Tabelle": "fixed mode: map + table, table only without location",
    "Ergebnis": "Result",
}


def _view_title(t: str) -> str:
    if t in VIEW_EXACT:
        return VIEW_EXACT[t]
    m = re.match(r"^(\d+) Treffer$", t)
    if m:
        return f"{m.group(1)} results"
    m = re.match(r"^(.+) im Jahresverlauf$", t)
    if m:
        return f"{_view_title(m.group(1))} over the year"
    m = re.match(r"^(.+?) nach (.+)$", t)
    if m:
        return f"{tr(m.group(1))} by " + " and ".join(tr(g).lower() for g in m.group(2).split(" und "))
    return tr(t)


def localize_view(view: dict | None, lang: str | None) -> dict | None:
    """View spec (presentation.py) in the requested language: title and rationale only."""
    if not view or (lang or "de").lower().startswith("de"):
        return view
    view = dict(view)
    if isinstance(view.get("title"), str):
        view["title"] = _view_title(view["title"])
    if isinstance(view.get("rationale"), str):
        view["rationale"] = VIEW_EXACT.get(view["rationale"], view["rationale"])
    return view
