"""Register of the data sources behind every chat answer (paper: "each record carries metadata on source,
status and accuracy, visible in the answers").

Tools list the source ids they used in their result frame; the frontend shows them as a source line under
the answer. Fields that are not known yet are None on purpose rather than guessed -- see OPEN_QUESTIONS.
"""

SOURCES: dict[str, dict] = {
    "energymap_heating": {
        "label": "EnergyMap Berlin, Heizwärmebedarf je Gebäude",
        "tables": ["emp.emblive (cons_c2r2, spec_cons_c2r2)"],
        "status": "2026-06-25, kalibrierte Simulation",  # emp.emblive.last_updated
        "method": "Gebäudesimulation, kalibriert; Variante c2r2",
        "accuracy": None,
        "reference_area": "Nettoraumfläche (spec = cons / nrf_area)",
        "license": None,
        "estimated": False,
    },
    "cooling_placeholder": {
        "label": "Kältebedarf, generischer Platzhalter je Nutzungsart",
        "tables": ["core_bldg.usage_cooling_profile"],
        "status": None,
        "method": "spezifischer Jahreskältebedarf je Nutzungsart × Nutzfläche; Monatsverteilung für alle "
                  "Nutzungsarten gleich (Mai 10 %, Jun 20 %, Jul 30 %, Aug 30 %, Sep 10 %). Keine Simulation.",
        "accuracy": "grobe Abschätzung",
        "reference_area": "PrimaryUsageZoneArea, sonst Heated area",
        "license": None,
        "estimated": True,
    },
    "alkis_function": {
        "label": "ALKIS-Gebäudefunktion (GFK)",
        "tables": ["emc.building (gfk)", "core_bldg.kww_gfk_mapping"],
        "status": None,
        "method": "amtliche Gebäudefunktion aus dem Liegenschaftskataster, vierstelliger Schlüssel",
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "kww_heat": {
        "label": "Wärmebedarf nach KWW-Technikkatalog Wärmeplanung (08-2026, V1.1), Stand 2025",
        "tables": ["core_bldg.kww_heat_demand_building", "core_bldg.kww_spec_demand"],
        "status": "2026-11-16, Tab 53–55",
        "method": "Bruttogeschossfläche (Grundfläche × oberirdische Geschosse) × Endenergiekennwert Heizung + "
                  "Warmwasser je Gebäudetyp und Baualtersklasse; Gebäudetyp aus der ALKIS-Gebäudefunktion. "
                  "Bundesweite Mittelwerte aus Energieausweisen, ohne Prozesswärme.",
        "accuracy": "Kennwertverfahren, bundesweite Mittelwerte",
        "reference_area": "Bruttogeschossfläche",
        "license": None,
        "estimated": True,
    },
    "kww_process_heat": {
        "label": "Prozesswärme-Relevanz nach KWW-Technikkatalog (Tab 56)",
        "tables": ["core_bldg.kww_process_heat", "core_bldg.kww_gfk_mapping"],
        "status": "2026-11-16",
        "method": "Einstufung je Gebäudefunktion (Ja/Nein), keine Mengenangabe",
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "state_property": {
        "label": "Berliner Landesgrundvermögen (Grundstücke des Landes Berlin)",
        "tables": ["core_bldg.state_property_parcel", "core_bldg.building_ownership"],
        "status": "2026-11-17 geladen (gdi.berlin.de WFS landesgrundvermoegen)",
        "method": "Gebäude mit mindestens 50 % der Grundfläche auf einem landeseigenen Flurstück; Aussage über das "
                  "Grundstück, nicht über das Gebäude (z. B. Lauben in Kleingärten auf Bezirksland)",
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "pv_potential": {
        "label": "Solarpotenzial Berlin (Solarrechner), geeignete Dachflächen je Gebäude",
        "tables": ["core_bldg.pv_potential_building"],
        "status": "2026-11-17 geladen (gdi.berlin.de WFS ua_solarpotenzial_solarrechner)",
        "method": "Summe der als geeignet eingestuften Dachflächen aus ALKIS und LoD2, potenzielle Leistung in kWp",
        "accuracy": None,
        "reference_area": None,
        "license": "Datenlizenz Deutschland Zero 2.0",
        "estimated": True,
    },
    "pv_installed": {
        "label": "PV-Bestandsanlagen, Umweltatlas Berlin (Basis Marktstammdatenregister)",
        "tables": ["core_bldg.pv_installation", "core_bldg.pv_installed_building", "core_base.pv_small_plz"],
        "status": "2026-11-17 geladen (gdi.berlin.de WFS ua_solaranlagen_pv)",
        "method": "Anlagen > 30 kWp und Anlagen der öffentlichen Hand als Standorte, dem Gebäude zugeordnet; "
                  "kleine Anlagen nur je Postleitzahl",
        "accuracy": "kleine private Anlagen fehlen je Gebäude",
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "data_centers": {
        "label": "Rechenzentren aus OpenStreetMap und PeeringDB",
        "tables": ["core_bldg.data_center", "core_bldg.data_center_building"],
        "status": "2026-11-17 geladen",
        "method": "kartierte bzw. gemeldete Standorte, dem Gebäude zugeordnet; nicht vollständig",
        "accuracy": "unvollständig",
        "reference_area": None,
        "license": "ODbL (OpenStreetMap)",
        "estimated": False,
    },
    "building_params": {
        "label": "Gebäudeparameter (Nutzung, Baujahr, Flächen)",
        "tables": ["emc.param_building"],
        "status": None,
        "method": None,
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "footprints": {
        "label": "Gebäudegrundrisse",
        "tables": ["emc.building"],
        "status": None,
        "method": None,
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "lod2": {
        "label": "3D-Gebäudemodell LoD2 Berlin",
        "tables": ["citydb (3DCityDB v4)", "emc.relation_bldg_citygml_v2"],
        "status": None,
        "method": "Zuordnung 2D↔3D: Verknüpfungstabelle, räumlicher Rückfall, sonst extrudierter Grundriss "
                  "(als geschätzt markiert)",
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "trees": {
        "label": "Baumbestand Berlin (Straßen- und Anlagenbäume)",
        "tables": ["core_veg.strassenbaeume", "core_veg.anlagenbaeume"],
        "status": None,
        "method": None,
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "chillers": {
        "label": "Rückkühler auf Dächern, aus Luftbildern erkannt",
        "tables": ['core_building."Chillers"'],
        "status": None,
        "method": "Objekterkennung in Luftbildern, mit Konfidenz je Objekt",
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
    "places": {
        "label": "Ortsnamen aus OpenStreetMap (Nominatim)",
        "tables": [],
        "status": None,
        "method": "Ortsname wird in Berlin nachgeschlagen; übertragen wird nur der Name",
        "accuracy": None,
        "reference_area": None,
        "license": "ODbL",
        "estimated": False,
    },
    "districts": {
        "label": "Bezirksgrenzen Berlin",
        "tables": ["emc.district"],
        "status": None,
        "method": None,
        "accuracy": None,
        "reference_area": None,
        "license": None,
        "estimated": False,
    },
}

# What still has to be filled in by hand (Steffi), kept here so it is not forgotten.
OPEN_QUESTIONS = [
    "Stand und Lizenz je Quelle (ALKIS-Grundrisse, LoD2, Baumbestand, Rückkühler-Erkennung)",
    "Genauigkeitsangabe EnergyMap-Heizwärmebedarf (z. B. Abweichung gegen Verbrauchswerte aus der Kalibrierung)",
    "Bedeutung der Variante c2r2 (Klima/Sanierung) in Worten für die Quellenzeile",
]


def describe(source_ids: list[str]) -> list[dict]:
    """Source entries for a result frame, in the given order, unknown ids skipped."""
    return [{"id": sid, **SOURCES[sid]} for sid in source_ids if sid in SOURCES]
