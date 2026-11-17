"""Import KWW-Technikkatalog Wärmeplanung (Tab 53–56) nach core_bldg.kww_*.

Liest die Tabellenblätter direkt (nicht flatdata_all), weil dort die
Reduktionsraten einzelner NWG-Typen von den Tabellenblättern abweichen. Die
2025-Werte werden gegen flatdata_all gegengeprüft.

Aufruf (Schema vorher mit 01_schema_kww.sql anlegen):
    python 02_import_kww.py <pfad/zur/KWW-Technikkatalog.xlsx> [--dsn postgresql://...]

Ohne --dsn wird DATABASE_URL aus ../../.env (backend-cesium) gelesen.
Benötigt: openpyxl, psycopg (v3).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import openpyxl
import psycopg

TYPE_KEYS = {
    "Industrie": "industrie",
    "Krankenhäuser": "krankenhaeuser",
    "Beherbergung, Gaststätten, Heime": "beherbergung_gaststaetten",
    "Kultureinrichtungen": "kultureinrichtungen",
    "Sporteinrichtungen (ohne Schwimmbäder)": "sporteinrichtungen",
    "Schwimmbäder": "schwimmbaeder",
    "Bildung": "bildungseinrichtungen",
    "Büro": "buerogebaeude",
    "Handel (Kaufhäuser und Märkte)": "handel",
    "Gebäude für Lagerung": "lagergebaeude",
    "Ein- und Zweifamilienhaus": "ezfh",
    "Mehrfamilienhaus": "mfh",
}
TAB_SECTOR = {"Tab 53": "NWG", "Tab 54": "WG", "Tab 55": "WG"}
SCENARIOS = [  # Spalten D..G der Tabellenblätter
    ("2025", 2025),
    ("2045_o45", 2045),
    ("2045_uba", 2045),
    ("2045_fortschreibung", 2045),
]


def clean(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = re.sub(r"\s+", " ", v.replace("\n", " ")).strip()
        return v or None
    return v


def num(v):
    v = clean(v)
    if v is None or v in ("-", "k. A."):
        return None
    return float(v)


def age_bounds(label: str):
    s = label.replace("…", "–").replace("...", "–").replace("-", "–")
    if s == "Alle Klassen":
        return None, None
    if m := re.fullmatch(r"bis (\d{4})", s):
        return None, int(m[1])
    if m := re.fullmatch(r"ab (\d{4})", s):
        return int(m[1]), None
    if m := re.fullmatch(r"(\d{4})\s*–\s*(\d{4})", s):
        return int(m[1]), int(m[2])
    raise ValueError(f"Baualtersklasse nicht erkannt: {label!r}")


def read_building_tab(ws, tab):
    """Liefert types, demand, reduction, notes aus Tab 53/54/55."""
    rows = [[clean(c) for c in r[1:9]] for r in ws.iter_rows(values_only=True)]
    types, demand, reduction, notes = {}, [], [], {}
    scen_labels = None
    current = None
    pending_reduction = []  # Tab 53: Reduktion steht vor den Typen und gilt für alle
    section = None
    for r in rows:
        b, c = r[0], r[1]
        if b is None:
            continue
        if b in ("Anmerkungen", "Quellen"):
            section = b
            continue
        if section == "Anmerkungen" and c:
            notes[b] = ("anmerkung", c)
            continue
        if section == "Quellen" and c:
            notes[b] = ("quelle", c)
            continue
        if b in ("Erwartungswerte", "Baualtersklasse"):
            scen_labels = r[2:6]
            continue
        if b == "Gebäudetyp":
            current = TYPE_KEYS[c]
            types[current] = {"label": c, "tab": tab, "sector": TAB_SECTOR[tab], "note_codes": None}
            for red in pending_reduction:
                reduction.append({**red, "kww_type": current})
            continue
        if b in ("Mittlere jährliche Reduktion", "Reduktion bis 2045 auf (2025 = 100%)"):
            key = "annual" if b.startswith("Mittlere") else "level"
            for (scen, _), v in zip(SCENARIOS[1:], r[3:6]):
                entry = {"scenario": scen, key: num(v), "notes": r[7] if key == "annual" else None}
                if current is None:
                    pending_reduction.append(entry)
                else:
                    reduction.append({**entry, "kww_type": current})
            continue
        if c == "kWh/(m²*a)" and current:
            y0, y1 = age_bounds(b)
            for (scen, _), v in zip(SCENARIOS, r[2:6]):
                demand.append({
                    "kww_type": current, "age_class": b.replace("...", "–"), "year_from": y0,
                    "year_to": y1, "scenario": scen, "value": num(v),
                    "notes": r[7], "sources": r[6],
                })
            if b == "Alle Klassen":
                types[current]["note_codes"] = r[7]
    # Kopfzeilen zur Kontrolle: Spalten müssen in der erwarteten Reihenfolge stehen
    assert scen_labels and str(scen_labels[0]) == "2025" and "O45" in scen_labels[1] \
        and "UBA" in scen_labels[2] and "Fortschreibung" in scen_labels[3], scen_labels
    return types, demand, reduction, notes, scen_labels


def merge_reduction(rows):
    out = {}
    for r in rows:
        k = (r["kww_type"], r["scenario"])
        e = out.setdefault(k, {"annual": None, "level": None, "notes": None})
        for f in ("annual", "level", "notes"):
            if r.get(f) is not None:
                e[f] = r[f]
    return out


def read_process_tab(ws):
    rows = [[c for c in r[1:7]] for r in ws.iter_rows(values_only=True)]
    out, notes, started, section = [], {}, False, None
    for r in rows:
        b = clean(r[0])
        if b is None:
            continue
        if b == "Gebäudefunktion":
            started = True
            continue
        if b in ("Anmerkungen", "Quellen"):
            section = b
            continue
        if section:
            if clean(r[1]):
                notes[b] = ("anmerkung" if section == "Anmerkungen" else "quelle", clean(r[1]))
            continue
        if not started:
            continue

        def yn(v):  # 'Ja\nNein' -> 'Ja/Nein'
            v = v.strip() if isinstance(v, str) else v
            return None if not v else "/".join(p.strip() for p in v.splitlines() if p.strip())

        out.append({
            "label": b,
            "schlomann": clean(r[1]),
            "iwu": "; ".join(p.strip() for p in str(r[2]).splitlines() if p.strip()) if r[2] else None,
            "ph": yn(r[3]), "dc": yn(r[4]), "pre": clean(r[5]),
        })
    return out, notes


def factor_from_notes(codes, notes):
    for code in (codes or "").replace(" ", "").split(","):
        txt = notes.get(code, ("", ""))[1]
        if m := re.search(r"Nutzfläche = (\d+(?:,\d+)?)", txt):
            return float(m[1].replace(",", "."))
    return None


def crosscheck_flatdata(ws, demand):
    flat = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[0] and str(r[0]).startswith("spezifischer_eev_"):
            flat[(r[1], str(r[5]).replace("...", "–").replace("…", "–"))] = r[6]
    bad = 0
    for d in demand:
        if d["scenario"] != "2025":
            continue
        f = flat.get((d["kww_type"], d["age_class"].replace("...", "–")))
        fv = None if f in (None, "k. A.") else float(f)
        if (fv is None) != (d["value"] is None) or (fv is not None and abs(fv - d["value"]) > 1e-6):
            bad += 1
            print(f"  ABWEICHUNG flatdata: {d['kww_type']} {d['age_class']}: Tab={d['value']} flat={f}")
    print(f"Gegenprüfung flatdata_all (2025): {bad} Abweichungen")


def dsn_from_env_file() -> str:
    env = Path(__file__).resolve().parents[2] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().replace("postgresql+psycopg://", "postgresql://")
    raise SystemExit("DATABASE_URL nicht in .env gefunden, bitte --dsn angeben")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--dsn")
    a = ap.parse_args()
    wb = openpyxl.load_workbook(a.xlsx, read_only=True, data_only=True)

    types, demand, red_rows, notes = {}, [], [], {}
    for tab in ("Tab 53", "Tab 54", "Tab 55"):
        t, d, r, n, _ = read_building_tab(wb[tab], tab)
        types.update(t); demand += d; red_rows += r; notes.update(n)
    reduction = merge_reduction(red_rows)
    process, n56 = read_process_tab(wb["Tab 56"])
    notes.update(n56)
    crosscheck_flatdata(wb["flatdata_all"], demand)

    with psycopg.connect(a.dsn or dsn_from_env_file()) as con, con.cursor() as cur:
        cur.execute("TRUNCATE core_bldg.kww_gfk_mapping, core_bldg.kww_spec_demand, core_bldg.kww_reduction, "
                    "core_bldg.kww_process_heat, core_bldg.kww_bldg_type, core_bldg.kww_scenario, core_bldg.kww_note")
        cur.executemany("INSERT INTO core_bldg.kww_note VALUES (%s,%s,%s)",
                        [(k, v[0], v[1]) for k, v in sorted(notes.items())])
        labels = {"2025": "2025", "2045_o45": "2045 gem. O45-Szenario",
                  "2045_uba": "2045 gem. UBA-Projektion", "2045_fortschreibung": "2045 gem. Fortschreibung"}
        cur.executemany("INSERT INTO core_bldg.kww_scenario VALUES (%s,%s,%s,%s)",
                        [(s, labels[s], y, i) for i, (s, y) in enumerate(SCENARIOS, 1)])
        cur.executemany(
            "INSERT INTO core_bldg.kww_bldg_type VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(k, v["label"], v["sector"], v["tab"], factor_from_notes(v["note_codes"], notes), v["note_codes"], i)
             for i, (k, v) in enumerate(types.items(), 1)])
        cur.executemany(
            "INSERT INTO core_bldg.kww_spec_demand VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            [(d["kww_type"], d["age_class"], d["year_from"], d["year_to"], d["scenario"], d["value"],
              d["notes"], d["sources"]) for d in demand])
        cur.executemany(
            "INSERT INTO core_bldg.kww_reduction VALUES (%s,%s,%s,%s,%s)",
            [(k[0], k[1], v["annual"], v["level"], v["notes"]) for k, v in reduction.items()])
        cur.executemany(
            "INSERT INTO core_bldg.kww_process_heat VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(p["label"], p["schlomann"], p["iwu"], p["ph"], p["dc"], p["pre"], i) for i, p in enumerate(process, 1)])
        con.commit()
    print(f"Importiert: {len(types)} Gebäudetypen, {len(demand)} spezifische Werte, "
          f"{len(reduction)} Reduktionspfade, {len(process)} Prozesswärme-Funktionen, {len(notes)} Anmerkungen/Quellen")
    print("Hinweis: kww_gfk_mapping wurde geleert -> jetzt 03_gfk_kww_mapping.sql ausführen.")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
