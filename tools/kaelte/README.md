# Ist-Kälte und Kältestrom aus erkannten Kälteanlagen

## Ablauf bei einer neuen Anlagenliste

```
python load_chillers.py <KIT-Liste.json>                  # Esri-JSON, EPSG:25833 -> core_bldg."Chillers"
python kaelte_ist_schaetzung.py                           # Zuordnung + Ist-Kälte/-Strom (Sekunden)
```

`load_chillers.py` ersetzt die bisherige Liste (mit `--keep-as <Name>` wird sie stattdessen aufgehoben).
Die Liste vom 17.11.2026 (`Berlin_MRCNN2026_2_Chillers_CoolingCapacities_1.json`, 5.891 Anlagen) bringt je Anlage
`capacity_regression_kw` (Regression), `capacity_kw` (Monte-Carlo-Mittel) und `capacity_sd_kw` (Monte-Carlo-Standardabweichung),
aber keine Erkennungssicherheit mehr (`confidence` = NULL).

## Rechenweg

```
Nennkälteleistung P   = capacity_kw der Anlagenliste, Band P ± capacity_sd_kw      (ohne Liste: Fläche × kW/m² je Typ)
Kälte E_th            = P / Überdimensionierung f × Volllaststunden VLH    (core_bldg.cooling_usage_group)
Strom E_el            = E_th / EER je Anlagentyp                           (core_bldg.chiller_type_param)
Band min              = P_min / f_max × VLH_min,   Strom min = E_th,min / EER_max
Band max              = P_max / f_min × VLH_max,   Strom max = E_th,max / EER_min
```

Nutzungsgruppe für VLH: ALKIS-Gebäudefunktion des versorgten Gebäudes (`core_bldg.cooling_gfk_group`),
Rechenzentren (OSM/PeeringDB) überschreiben. Sichten auf das Ergebnis (`core_base.v_street_segment_load`) werden beim
Neulauf automatisch neu angelegt.

Die potenzielle Netzstruktur (`../kaeltenetz/load_strassennetz.py`) hängt nicht an den erkannten Anlagen.

## Zuordnung Anlage → Gebäude (`core_bldg.chiller_assignment`)

1. Trägergebäude: größte Überdeckung mit einem ALKIS-Umring, sonst nächstes Gebäude ≤ 15 m.
2. Träger in der Ver- und Entsorgung (GFK 2500–2622: Kraft-, Heiz-, Umspannwerke …) → `energieversorgung_ausgeschlossen`
   (Kühltürme der Energieerzeugung, keine Gebäudekühlung).
3. Träger unbeheizt (Parkhaus, Garage, Umformer …) → größtes beheiztes Gebäude ≤ 75 m als versorgtes Gebäude
   (`umgehaengt_von_unbeheizt`), sonst `traeger_unbeheizt_kein_ersatz`.
4. Gebäudeebene (`core_bldg.cooling_estimate_building`), `check_flags`:
   * `evtl_mehrere_gebaeude`: > 300 W Nennleistung je m² BGF (Rechenzentrum, Kühlhaus, Produktion: > 1000 W/m²)
   * `kuehlturm_evtl_prozesskaelte`: Kühlturm auf einem Gebäude ohne Produktions-/RZ-Funktion

Validierung (offen): Kältebedarf aus der BGF gegen die Ist-Schätzung, je Gebäude und je Baublock.

## Quellen und Annahmen

* Kälteleistungen je Anlage: KIT (Mask R-CNN 2026), Regression nach Barth et al. (2023), IBPSA Building Simulation,
  doi:10.26868/25222708.2023.1259, mit Monte-Carlo-Simulation.
* Barth, Dissertation KIT – Jahres-EER luftgekühlt 2,7 ± 0,6, wassergekühlt 3,9 ± 0,6; Rechenzentren Lastfaktor 0,6,
  28 % freie Kühlung.
* kW/m² je Typ nur noch Rückfall ohne Leistungsspalte (Median der KIT-Liste: ACC_Large 28, ACC_1_Fan 36, ACC_2_Fans 73,
  Kühltürme 84–96 kW/m²).
* Volllaststunden: Spannen aus Barth (New York, wärmer) für Berlin nach unten gesetzt, sonst Annahmen –
  **alle Werte in den Parametertabellen sind von Hand anpassbar** und werden beim nächsten Lauf nicht überschrieben.

Stand 17.11.2026 (KIT-Liste 2026): 5.891 Anlagen, 5.149 einem Gebäude zugeordnet, 229 Kraftwerkskühltürme
(214 MW) ausgeschlossen; 2.847 versorgte Gebäude, ~1,46 GW, Kälte ~1,3 TWh/a (Band 0,3–4,0), Strom ~0,47 TWh/a.
Vorher (Flächenschätzung, Liste 2025): 1.979 Gebäude, ~2,2 GW, Kälte ~1,8 TWh/a.

## Kältebedarf-Mockup mit Klima, Maßnahmen und Sollwert (`kaeltebedarf_mockup.sql`)

Platzhalter, bis die Projektpartner simulieren. Tabellen `core_bldg.cooling_age_class`, `cooling_spec_demand`
(kWh/(m²·a) Nutzkälte je GFK × Baualtersklasse, min/std/max, Bezug NGF, witterungsbereinigt), `climate_scenario`,
`climate_factor` (gering/mittel/stark, % je Stützjahr 2025–2050), `demand_measure` (außenliegende Verschattung,
Nutzerlüftung, automatische Nachtlüftung), `cooling_setpoint` (22/24/26 °C, % je K gegenüber dem Bezug nach
DIN V 18599-10), `heat_dhw_share`. Fast alle Werte: Annahme (Spalte `source`).

```sql
SELECT * FROM core_bldg.f_building_demand('stark', 2045, '{verschattung_aussen,nachtlueftung_auto}', 26);
```

Ergibt je beheiztem Gebäude Kälte min/std/max und Wärme (KWW, Pfad 2025 → 2045 linear, Klima nur auf Raumwärme).
Berlin 2025 mittel: Kälte 3,3 TWh/a (1,2–7,8), Wärme 23,8 TWh/a; ganze Stadt in ~13 s.
Neulauf des Skripts setzt alle Werte zurück.

## Wärme- und Kälteliniendichten (`../kaeltenetz/liniendichte.sql`)

Liniendichte = Jahresbedarf aller an einen Hauptnetz-Abschnitt angeschlossenen Gebäude je Meter, in kWh/(m·a) wie die
Berliner Wärmeliniendichten. `*_kwh_m_a` bezieht sich auf die Länge des Straßenabschnitts, `*_kwh_m_a_trasse` auf
Hauptleitung plus Anschlussleitungen. Bedarfe aus `core_bldg.f_building_demand`, Kälte also Mockup.

```sql
SELECT * FROM core_bldg.f_line_density('stark', 2045, '{nachtlueftung_auto}', 26);   -- beliebiges Szenario, ~13 s
SELECT * FROM core_base.v_line_density WHERE scenario = '2045_stark';                -- vorberechnet, mit Geometrie
CALL core_bldg.p_refresh_line_density();   -- alle Szenarien aus core_bldg.line_density_scenario neu (~1 min)
```

Nach neuen Bedarfen oder `load_strassennetz.py --sync` die Prozedur erneut aufrufen. Kurze Abschnitte mit großen
Gebäuden (Rechenzentren, Kaufhäuser) ergeben sehr hohe Einzelwerte.
