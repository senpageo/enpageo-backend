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
