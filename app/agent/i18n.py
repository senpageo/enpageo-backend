"""English output for the chat answers (the map's /en pages).

The tools build their result frames in German (labels, units, notes, source descriptions, category values).
Rather than threading a language through every tool, localize() translates a finished frame once, at the
API boundary. Unknown strings stay German, so a new tool never breaks, it is only not translated yet.
Usage types mirror website src/lib/cesiumMap/usage.ts.
"""
import copy
import re

EXACT = {
    "Hinweis: Dies sind ausgedachte Zahlen; sie werden bald durch Simulationsergebnisse ersetzt.":
        "Note: these numbers are made up; they will soon be replaced by simulation results.",
    "Die neuen Bäume sind nur ein Szenario in der Karte (lila) und werden nicht gespeichert. Straßenbreite, Leitungen im Boden, Zufahrten und Parkplätze sind nicht geprüft.":
        "The new trees are only a scenario on the map (purple) and are not saved. Street width, underground pipes, driveways and parking are not checked.",
    "neue Bäume": "new trees", "Straße": "Street", "Tage über 30 °C": "Days above 30 °C",
    "Stunden über 26 °C": "Hours above 26 °C", "höchste Raumtemperatur": "Highest room temperature",
    "Variante": "Variant", "ohne Sanierungen": "without retrofits", "mit Sanierungspfad": "with retrofit path",
    "sanierte Gebäude": "Retrofitted buildings",
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
    "Gebäude mit Kälteanlage": "Buildings with a chiller",
    "Erkannte Kälteanlagen": "Detected chillers",
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
    "Straße": "Street",
    "installierte Kälteleistung erkannter Anlagen (heute)": "Installed capacity of detected chillers (today)",
    "installiert heute im Verhältnis zu benötigt": "Installed today relative to required",
    "Wärmebelastung am Tag (PET 14 Uhr)": "Heat stress by day (PET 2 pm)",
    "Lufttemperatur nachts (4 Uhr)": "Night air temperature (4 am)",
    "Wärmebelastung am Tag (PET 14 Uhr, Mittel der Gebäude)": "Heat stress by day (PET 2 pm, mean of buildings)",
    "Lufttemperatur nachts (4 Uhr, Mittel der Gebäude)": "Night air temperature (4 am, mean of buildings)",
    "Bioklimatische Bewertung": "Bioclimatic rating", "Bewertung": "Rating", "Größe": "Size",
    "sehr ungünstig belastetes Gebiet": "Area with very unfavourable heat stress",
    "stadtklimatisch vulnerables Gebiet": "Climatically vulnerable area",
    "Block(teil)fläche": "Block (part)", "Fläche": "Area", "Aufenthaltsqualität am Tag": "Comfort by day",
    "Siedlungsfläche": "Settlement area", "Verkehrsfläche": "Street area", "Grün-/Freifläche": "Green/open space",
    "Wärmebelastung im Außenraum für künftige Jahre und Klimaszenarien": "Outdoor heat stress for future years and climate scenarios",
    "Art": "Type", "Name": "Name", "Betreiber / Ansprechpartner": "Operator / contact", "Entfernung": "Distance",
    "Kälteleistung": "Cooling capacity", "Jahresarbeit": "Annual energy", "Trassenlänge": "Route length",
    "Vor-/Rücklauf": "Supply/return", "Hinweis": "Note", "Quelle": "Source", "Block": "Block",
    "Kältenetz": "District cooling network", "Nahwärmenetz": "Local heating network",
    "Fern-/Nahwärmeversorgung 2022": "District/local heating supply 2022",
    "Stromverbrauch 2021": "Electricity consumption 2021", "Kältestrom erkannter Anlagen": "Electricity of detected chillers",
    "Anteil Kältestrom (erkannt)": "Share chiller electricity (detected)",
    "Anteil Kältestrom (Bedarf)": "Share chiller electricity (demand)",
    "installierte Kälteleistung": "Installed cooling capacity", "installiert min": "installed min",
    "installiert max": "installed max", "Kälteüberschuss": "Cooling surplus", "Nachbarn ohne Anlage": "Neighbours without unit",
    "deren benötigte Leistung": "their required capacity", "deren Kältebedarf": "their cooling demand",
    "nächster Nachbar": "nearest neighbour", "Überschuss deckt Nachbarn zu": "Surplus covers neighbours by",
    "Leitungsverläufe bestehender Wärme- und Kältenetze": "Routes of existing heating and cooling networks",
    "Strom nach Verbrauchern (Haushalt, Gewerbe) und Lastgänge": "Electricity by consumer (households, business) and load profiles",
    "Jahr": "Year",
    "Klimaszenario": "Climate scenario",
    "Sollwert": "Setpoint",
    "Maßnahmen": "Measures",
    "geringer Klimawandel": "low climate change",
    "mittlerer Klimawandel": "medium climate change",
    "starker Klimawandel": "strong climate change",
    "ohne Maßnahmen": "without measures",
    "Sollwert nach Gebäudefunktion": "setpoint per building function",
    "Kältestrom bei Deckung des Bedarfs": "Chiller electricity if the demand is met",
    "elektrische Leistung der Kälteerzeugung": "Electric power of cooling generation",
    "Abwärme der Kälteerzeugung (aus Bedarf)": "Waste heat of cooling generation (from demand)",
    "Abwärme erkannter Kälteanlagen": "Waste heat of detected chillers",
    "Abwärmeleistung erkannter Kälteanlagen": "Waste heat output of detected chillers",
    "Anteil des Bedarfs, den erkannte Anlagen decken": "Share of demand met by detected units",
    "Planungsraum (Nr.)": "Planning area (no.)",
    "mittlerer U-Wert (typisch)": "Mean U-value (typical)",
    "mittlerer U-Wert (typisch, Mittel der Gebäude)": "Mean U-value (typical, mean of buildings)",
    "U-Wert Wand": "U-value wall", "U-Wert Dach": "U-value roof", "U-Wert Boden": "U-value floor",
    "U-Wert Fenster": "U-value window", "U-Wert Wand ±": "U-value wall ±", "U-Wert Dach ±": "U-value roof ±",
    "U-Wert Boden ±": "U-value floor ±", "U-Wert Fenster ±": "U-value window ±",
    "U-Wert Wand (typisch)": "U-value wall (typical)", "U-Wert Fenster (typisch)": "U-value window (typical)",
    "Quelle U-Werte": "U-value source",
    "TABULA DE (Typgebäude Ist-Zustand)": "TABULA DE (type building, current state)",
    "IWU-NWG (Mittel der erhobenen Gebäude)": "IWU-NWG (mean of surveyed buildings)",
    "Typische U-Werte nach Gebäudetyp und Baualter (NaiS-DB: TABULA DE, IWU-NWG)":
        "Typical U-values by building type and age (NaiS-DB: TABULA DE, IWU-NWG)",
    "Tatsächlicher Sanierungsstand und gemessene U-Werte je Gebäude":
        "Actual retrofit state and measured U-values per building",
    "Sind nicht im Datenbestand; typische U-Werte nach Gebäudetyp und Baualter (NaiS-DB) sind da":
        "Not in the dataset; typical U-values by building type and age (NaiS-DB) are available",
    "U-Werte: typische Werte nach Gebäudetyp und Baualter aus der NaiS-DB (Wohnen TABULA-Typgebäude im Ist-Zustand, Nichtwohnen IWU-NWG-Mittelwerte mit Standardfehler); der tatsächliche Sanierungsstand je Gebäude ist nicht bekannt.":
        "U-values: typical values by building type and age from the NaiS-DB (residential: TABULA type buildings in current state, non-residential: IWU-NWG means with standard error); the actual retrofit state of each building is not known.",
    "Kältestrom = Kältebedarf / Leistungszahl der Kälteerzeugung je Nutzung (luftgekühlt 2,7, wassergekühlt 3,9; Annahme), wenn der ganze Bedarf mit Kälteanlagen gedeckt wird.":
        "Chiller electricity = cooling demand / EER of cooling generation per usage (air-cooled 2.7, water-cooled 3.9; assumption), if the whole demand is met by chillers.",
    "Keine beheizten Gebäude mit Kältebedarf in dieser Auswahl.": "No heated buildings with cooling demand in this selection.",
    "Kältebedarf": "Cooling demand",
    "spez. Kältebedarf": "Specific cooling demand",
    "Kältebedarf min": "Cooling demand min",
    "Kältebedarf max": "Cooling demand max",
    "benötigte Kälteleistung": "Required cooling capacity",
    "Kälteliniendichte": "Cooling line density",
    "Kältedichte": "Cooling density",
    "Dichte der benötigten Kälteleistung": "Required cooling capacity density",
    "Kälteverbrauch (aus installierter Leistung)": "Cooling consumption (from installed capacity)",
    "Kälteverbrauch (aus installierter Leistung, Abgleich)": "Cooling consumption (from installed capacity, cross-check)",
    "Kälteverbrauch erkannter Anlagen (Abgleich)": "Cooling consumption of detected units (cross-check)",
    "Kälteverbrauch min": "Cooling consumption min",
    "Kälteverbrauch max": "Cooling consumption max",
    "installierte Kälteleistung erkannter Anlagen": "Installed capacity of detected units",
    "Gebäude mit erkannter Anlage": "Buildings with detected unit",
    "Kältebedarf ohne die Maßnahmen": "Cooling demand without the measures",
    "Benötigte Kälteleistung = Kältebedarf / Volllaststunden der Nutzung (Annahme).":
        "Required cooling capacity = cooling demand / full-load hours of the usage (assumption).",
    "Die Monatsverteilung ist eine pauschale Annahme (Mai–September), für alle Nutzungsarten gleich.":
        "The monthly split is a flat assumption (May-September), the same for all usage types.",
    "Kälteverbrauch = Schätzung aus der installierten Leistung erkannter Kälteanlagen (Luftbild) mit Volllaststunden, nur für Gebäude mit erkannter Anlage; Abgleich zum Kältebedarf, große Unsicherheit.":
        "Cooling consumption = estimate from the installed capacity of detected chillers (aerial imagery) with full-load hours, only for buildings with a detected unit; cross-check of the demand, large uncertainty.",
    "Wärmeliniendichte": "Heat line density",
    "Kälteliniendichte (Platzhalter)": "Cooling line density (placeholder)",
    "Wärmebedarf (KWW)": "Heat demand (KWW)",
    "Abschnittslänge": "Section length",
    "Kältebedarf-Mockup je Gebäudefunktion und Baualtersklasse (Annahme)":
        "Cooling demand mock-up per building function and age class (assumption)",
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
    "Kälteleistung erkannter Anlagen (geschätzt)": "Capacity of detected cooling units (estimated)",
    "Ist-Kälte (Schätzung, Mittel)": "Actual cooling (estimate, mean)",
    "Ist-Kälte min": "Actual cooling min", "Ist-Kälte max": "Actual cooling max",
    "Kältestrom (Schätzung, Mittel)": "Cooling electricity (estimate, mean)",
    "Kältestrom min": "Cooling electricity min", "Kältestrom max": "Cooling electricity max",
    "Zuordnung prüfen": "Check assignment",
    "Anschluss an Straße": "Connection to street",
    "Ist-Kälte erkannter Anlagen (Schätzung, Mittel)": "Actual cooling of detected units (estimate, mean)",
    "Ist-Kälte erkannter Anlagen (Schätzung, Mittel) – min": "Actual cooling of detected units – min",
    "Ist-Kälte erkannter Anlagen (Schätzung, Mittel) – max": "Actual cooling of detected units – max",
    "Kältestrom (Schätzung, Mittel) – min": "Cooling electricity – min",
    "Kältestrom (Schätzung, Mittel) – max": "Cooling electricity – max",
    "Kälteleistung erkannter Anlagen (geschätzt) – min": "Capacity of detected units – min",
    "Kälteleistung erkannter Anlagen (geschätzt) – max": "Capacity of detected units – max",
    "Ist-Kälte und Kältestrom aus erkannten Kälteanlagen (Schätzung)": "Actual cooling and cooling electricity from detected units (estimate)",
    "große Unsicherheit, Spanne min–max angeben": "large uncertainty, state the min–max range",
    "Potenzielle Netzstruktur: Detailnetz Berlin (Straßenabschnitte) mit Anschlussleitung je beheiztem Gebäude": "Potential network structure: Berlin street network (Detailnetz) with a connection line per heated building",
    "Ist-Kälteverbrauch und Kältestrom sind grobe Schätzungen aus den im Luftbild erkannten Kälteanlagen "
    "(Kälteleistung je Anlage aus der KIT-Liste mit Spanne, Überdimensionierung, Volllaststunden je Gebäudefunktion, Leistungszahl je "
    "Anlagentyp). Die Unsicherheit ist groß, deshalb immer die Spanne min–max mit angeben. Anlagen versorgen oft "
    "Nachbargebäude mit; die Zuordnung ist noch nicht validiert.":
        "Actual cooling and cooling electricity are rough estimates from cooling units detected in aerial images "
        "(capacity per unit from the KIT list with range, oversizing, full-load hours per building function, efficiency per unit type). "
        "Uncertainty is large, so always state the min–max range. Units often also supply neighbouring buildings; "
        "the assignment is not validated yet.",
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
    "Objekterkennung in Luftbildern (KIT); Kälteleistung je Anlage über Regression und Monte-Carlo-Simulation (Barth et al. 2023)":
        "Object detection in aerial images (KIT); cooling capacity per unit via regression and Monte Carlo simulation (Barth et al. 2023)",
    "Aus Luftbildern erkannt.": "Detected from aerial images.",
    "luftgekühlt": "air-cooled",
    "luftgekühlt (klein)": "air-cooled (small)",
    "Hybridkühler (vermutet)": "hybrid (assumed)",
    "Kühlturm": "cooling tower",
    "Kühlturm (unsicher erkannt)": "cooling tower (uncertain)",
    "0 kW heißt: im Luftbild wurde kein Lüfter erkannt, daher keine Leistungsschätzung – nicht, dass die Anlage keine Leistung hat.":
        "0 kW means no fan was detected in the aerial image, so there is no capacity estimate – not that the unit has no capacity.",
    "Kälteleistung je Anlage: Schätzung des KIT aus dem Luftbild (Mittelwert einer "
    "Monte-Carlo-Simulation, Standardabweichung als Unsicherheit), keine gemessenen Werte.":
        "Cooling capacity per unit: KIT estimate from aerial images (mean of a Monte Carlo simulation, standard "
        "deviation as uncertainty), not measured values.",
    "Lüfter": "Fans",
    "Stück": "pcs",
    "Kälteleistung (KIT-Schätzung)": "Cooling capacity (KIT estimate)",
    "Kälteleistung – Standardabweichung": "Cooling capacity – standard deviation",
    "gemessene Kälteleistungen je erkannter Anlage": "Measured cooling capacities per detected unit",
    "es gibt die KIT-Schätzung je Anlage aus dem Luftbild (Metrik chiller_capacity, mit Spanne; find_chillers je Anlage)":
        "the KIT estimate per unit from aerial images is available (metric chiller_capacity, with range; find_chillers per unit)",
    "Ortsnamen aus OpenStreetMap (Nominatim)": "Place names from OpenStreetMap (Nominatim)",
    "Ortsname wird in Berlin nachgeschlagen; übertragen wird nur der Name":
        "The place name is looked up in Berlin; only the name is transmitted",
    "Bezirksgrenzen Berlin": "Berlin district boundaries",
    # missing data (report_missing_data: "Fehlt noch: <what>. <Why>.")
    "Klimaszenarien und Zukunftsjahre": "Climate scenarios and future years",
    "Wirkungen von Maßnahmen und Sanierungen auf den Kältebedarf": "Effects of measures and retrofits on cooling demand",
    "Simulierte Klimaszenarien und Zukunftsjahre": "Simulated climate scenarios and future years",
    "Simulierte Wirkungen von Maßnahmen und Sanierungen auf den Kältebedarf":
        "Simulated effects of measures and retrofits on cooling demand",
    "Kommen aus der Simulation im Projekt; Annahmen (Mockup) je Gebäude gibt es schon (building_scenario_demand)":
        "Will come from the simulation in the project; assumptions (mock-up) per building are already available",
    "Kommen aus der Simulation im Projekt; Annahmen (Mockup) für Verschattung und Lüftung gibt es schon (building_scenario_demand)":
        "Will come from the simulation in the project; assumptions (mock-up) for shading and ventilation are already available",
    "Adresse": "Address",
    "min": "min", "max": "max",
    "mit Maßnahmen": "with measures",
    "mit Maßnahmen min": "with measures min",
    "mit Maßnahmen max": "with measures max",
    "Einsparung": "Saving",
    "Rechenzentren und Kühlhäuser (Prozesskälte) sind nicht in der Rangfolge, weil die Maßnahmen dort nicht wirken.":
        "Data centres and cold stores (process cooling) are left out of the ranking, as the measures do not act there.",
    "Die Maßnahmen wirken in den Annahmen nur auf den Kältebedarf.":
        "In the assumptions, the measures only act on cooling demand.",
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


def _scenario_en(s: str) -> str:
    return (s.replace("geringer Klimawandel", "low climate change").replace("mittlerer Klimawandel", "medium climate change")
             .replace("starker Klimawandel", "strong climate change").replace("alle Maßnahmen", "all measures")
             .replace("Sollwert nach Gebäudefunktion", "setpoint per building function")
             .replace("Sollwert", "setpoint").replace("ohne Maßnahmen", "without measures")
             .replace("Maßnahmen: ", "measures: ").replace("Standardszenario", "standard scenario")
             .replace("Standard (", "standard (")
             .replace("außenliegende Fensterverschattung", "external window shading")
             .replace("Lüftung durch Nutzer", "ventilation by users")
             .replace("automatische Nachtlüftung", "automatic night ventilation")
             .replace("Fenstertausch", "window replacement").replace("Sanierung der Hülle", "envelope retrofit"))


def _measures_en(s: str) -> str:
    return (s.replace("außenliegende Fensterverschattung", "external window shading")
             .replace("Lüftung durch Nutzer (Fensterlüftung in kühlen Stunden)", "ventilation by users (cool hours)")
             .replace("automatische Nachtlüftung", "automatic night ventilation")
             .replace("Fenstertausch mit Sonnenschutzverglasung", "window replacement with solar control glazing")
             .replace("energetische Sanierung der Hülle (Dämmung, Fenster)", "envelope retrofit (insulation, windows)")
             .replace("Fenstertausch", "window replacement").replace("Sanierung der Hülle", "envelope retrofit")
             .replace(" bis ", " to "))


PATTERNS = [
    (re.compile(r"^Es würden (\d+) Bäume gepflanzt werden \(alle (\d+) m auf beiden Straßenseiten, wo kein Baum und kein "
                r"Gebäude im Weg ist; (\d+) Bäume stehen dort schon\)\. Sie kühlen die Umgebungsluft um (\d+) °C ab\. Sie "
                r"verschatten die Fassaden von (\d+) Gebäuden und senken deren Kältebedarf um etwa ([\d,]+) MWh/a "
                r"\((\d+) %, Spanne (\d+) bis (\d+) %\)\.$"),
     lambda m: f"{m.group(1)} trees would be planted (every {m.group(2)} m on both sides of the street where no tree "
               f"or building is in the way; {m.group(3)} trees are already there). They cool the ambient air by "
               f"{m.group(4)} °C. They shade the facades of {m.group(5)} buildings and lower their cooling demand by "
               f"about {m.group(6).replace(',', '.')} MWh/a ({m.group(7)} %, range {m.group(8)} to {m.group(9)} %)."),
    (re.compile(r"^Davon (\d+) Anlagen mit 0 kW \(kein Lüfter erkannt, keine Leistungsschätzung\)\. Die kleinste "
                r"geschätzte Leistung liegt bei (\d+) kW\.$"),
     lambda m: f"Of these, {m.group(1)} units with 0 kW (no fan detected, no capacity estimate). The smallest "
               f"estimated capacity is {m.group(2)} kW."),
    (re.compile(r"^Anteil an (.+) gesamt$"), lambda m: f"Share of total {tr(m.group(1)).lower()}"),
    (re.compile(r"^Rasterzellen (\d+) m × (\d+) m, gezeigt werden die dichtesten (\d+)\. Kältewerte sind generische "
                r"Platzhalter, keine Simulation\.$"),
     lambda m: f"Grid cells {m.group(1)} m × {m.group(2)} m, showing the densest {m.group(3)}. Cooling values are "
               "generic placeholders, not a simulation."),
    (re.compile(r"^Szenario (.+)\. Liniendichte = Jahresbedarf der angeschlossenen beheizten Gebäude je Meter "
                r"(Haupt- und Anschlussleitung|Straßenabschnitt) auf der potenziellen Netzstruktur \(kein bestehendes Netz\)\.$"),
     lambda m: f"Scenario {_scenario_en(m.group(1))}. Line density = annual demand of the connected heated buildings "
               f"per metre of {'main and connection line' if m.group(2).startswith('Haupt') else 'street section'} "
               "on the potential network structure (not an existing network)."),
    (re.compile(r"^(Kältebedarf|Wärmebedarf \(KWW\)) (\d{4})$"),
     lambda m: f"{'Cooling demand' if m.group(1) == 'Kältebedarf' else 'Heat demand (KWW)'} {m.group(2)}"),
    (re.compile(r"^Szenario (.+?)(, Sollwert nach Nutzung \(DIN V 18599-10\))?\. Werte sind Annahmen \(Mockup\) bis zur "
                r"Simulation der Projektpartner, keine Simulation\.$"),
     lambda m: f"Scenario {_scenario_en(m.group(1))}{', setpoint by usage (DIN V 18599-10)' if m.group(2) else ''}. "
               "Values are assumptions (mock-up) until the project partners simulate, not a simulation."),
    (re.compile(r"^Maßnahmen: (.+)\. Wirkungen multiplizieren sich; Kühlhäuser und Rechenzentren ausgenommen\.$"),
     lambda m: f"Measures: {_measures_en(m.group(1))}. Effects multiply; cold stores and data centres excluded."),
    (re.compile(r"^(.+) – (min|max)$"), lambda m: f"{tr(m.group(1))} – {m.group(2)}"),
    (re.compile(r"^Entwicklung (\d{4})–(\d{4}); sonst wie (.+)\. Annahmen, keine Simulation\.$"),
     lambda m: f"Development {m.group(1)}–{m.group(2)}; otherwise {_scenario_en(m.group(3))}. Assumptions, not a simulation."),
    (re.compile(r"^Sonst wie (.+)\. Annahmen, keine Simulation\.$"),
     lambda m: f"Otherwise {_scenario_en(m.group(1))}. Assumptions, not a simulation."),
    (re.compile(r"^Abgleich: Kälteverbrauch erkannter Anlagen heute ([\d.]+) MWh/a an (\d+) Gebäuden\.$"),
     lambda m: f"Cross-check: consumption of detected chillers today {m.group(1).replace('.', ',')} MWh/a in {m.group(2)} buildings."),
    (re.compile(r"^(\d+) Gebäude eingefärbt( \(Obergrenze erreicht\))?; Median (.+), 10 %: (.+), 90 %: (.+)$"),
     lambda m: f"{m.group(1)} buildings coloured{' (limit reached)' if m.group(2) else ''}; median {m.group(3)}, "
               f"10 %: {m.group(4)}, 90 %: {m.group(5)}"),
    (re.compile(r"^Änderung gegenüber (.+)$"), lambda m: f"Change vs. {tr(m.group(1))}"),
    (re.compile(r"^((?:außenliegende Fensterverschattung|Lüftung durch Nutzer|automatische Nachtlüftung|Fenstertausch|Sanierung der Hülle)(?: \+ .+)?)$"),
     lambda m: _measures_en(m.group(1))),
    (re.compile(r"^(Adresse|Ort) aufgelöst als: (.+)$"),
     lambda m: f"{'Address' if m.group(1) == 'Adresse' else 'Place'} resolved as: {m.group(2)}"),
    (re.compile(r"^Kältebedarf: (Standardszenario|Szenario) (.+)\. Annahmen \(kWh/\(m²·a\) je Gebäudefunktion und "
                r"Baualter mit Korrekturfaktoren\), keine Simulation\.$"),
     lambda m: f"Cooling demand: {'standard scenario' if m.group(1) == 'Standardszenario' else 'scenario'} "
               f"{_scenario_en(m.group(2))}. Assumptions (kWh/(m²·a) per building function and age class with "
               "correction factors), not a simulation."),
    (re.compile(r"^Ohne die Maßnahmen: (.+)\. Mit: (.+)\. Annahmen, keine Simulation\.$"),
     lambda m: f"Without the measures: {_scenario_en(m.group(1))}. With: {_scenario_en(m.group(2))}. "
               "Assumptions, not a simulation."),
    (re.compile(r"^Maßnahmen: (.+)\. Wirkungen multiplizieren sich\.$"),
     lambda m: f"Measures: {_measures_en(m.group(1))}. Effects multiply."),
    (re.compile(r"^Median im Gebiet (\d+) kWh/\(m·a\), Gesamtmittel (\d+) kWh/\(m·a\)\.$"),
     lambda m: f"Median in the area {m.group(1)} kWh/(m·a), overall mean {m.group(2)} kWh/(m·a)."),
    (re.compile(r"^Je höher die Liniendichte, desto günstiger für ein Netz; eine Wirtschaftlichkeitsprüfung ersetzt "
                r"das nicht\.( Kältewerte sind Annahmen \(Mockup\), keine Simulation\.| Wärme nach KWW-Kennwerten "
                r"\(Heizung \+ Warmwasser\)\.)$"),
     lambda m: "The higher the line density, the more favourable for a network; it does not replace an economic "
               "assessment." + (" Cooling values are assumptions (mock-up), not a simulation." if "Kälte" in m.group(1)
                                else " Heat from KWW benchmarks (heating + hot water).")),
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
_VALUE_FIELDS = {"class", "chiller_class", "usage_type", "usage_zone_type", "construction_period", "month_label", "process_heat", "plot_owner", "climate", "scenario"}


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
    m = re.match(r"^(.+) über die Jahre$", t)
    if m:
        return f"{_view_title(m.group(1))} over the years"
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
