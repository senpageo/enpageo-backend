"""Baut aus dem zellweisen Export (export_region.py) ein 3D-Tiles-1.1-Tileset.

    python build_region.py data/neukoelln out/neukoelln [--jobs 4]

Schritt 1, global: Zuordnung LoD2-Körper -> 2D-Gebäude über alle Zellen (nur Kandidaten, klein im Speicher).
  bridge     Verknüpfungstabelle: 1:1 allein, sonst alle Teilkörper; jeder Körper an den besten Anspruch
  spatial    kein Körper aus der Tabelle: bester noch freier Körper mit >= 15 % Überlappung des Grundrisses
  covered    Grundriss zu >= 50 % unter bereits vergebenen Körpern (Seitenflügel/Anbau im Körper des
             Nachbarn): keine eigene Geometrie, Verweis cb auf den Besitzer des Körpers
  estimated  nichts davon: extrudierter Grundriss mit geschätzter Höhe
Schritt 2, je Zelle parallel: Geometrie bauen. Der Körper eines Gebäudes stammt immer aus den Kandidaten
  dieses Gebäudes und steht deshalb in derselben Zelldatei.

Kachelbaum (Ersatz statt Ergänzung):
  Wurzel -> Übersicht 4 km (vereinfachte Grundrisse >= 100 m²) -> grob 1 km (Grundrisse >= 40 m², vereinfacht)
         -> fein 250 m (LoD2, Boxen für estimated)
Attribute je 1-km-Zelle in attrs/<i>_<j>.json, für die Übersicht verschlankt (nur Stufe + Heizwert) in
attrs/overview_<oi>_<oj>.json; im tileset.json je Kachel unter extras.attrs verlinkt.
"""
import argparse
import csv
import io
import json
import math
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from glb import MeshBuilder, cols, extrude, mat, region_of, write_glb

CELL = 1000.0
FINE = 250.0
OVERVIEW_CELLS = 4  # Übersichtskachel = 4 x 4 Zellen
OVERVIEW_MIN_AREA = 100.0  # m²
COARSE_MIN_AREA = 40.0  # m²; Schuppen/Garagen erst in der feinen Stufe
GEOMETRIC_ERROR = {"root": 400.0, "overview": 48.0, "coarse": 16.0, "fine": 0.0}
SPATIAL_MIN, COVERED_MIN = 0.15, 0.5

csv.field_size_limit(2**31 - 1)


def sections(text):
    parts = {}
    cur, buf = None, []
    for line in text.splitlines(keepends=True):
        if line.startswith("#") and line.strip()[1:].isupper():
            if cur:
                parts[cur] = "".join(buf)
            cur, buf = line.strip()[1:], []
        else:
            buf.append(line)
    if cur:
        parts[cur] = "".join(buf)
    return parts


def rows(part):
    return list(csv.DictReader(io.StringIO(part))) if part and part.strip() else []


# ---------------------------------------------------------------------------------------------- Schritt 1

def assign(data_dir: Path):
    area, cell_of = {}, {}
    bridge = defaultdict(list)
    overlap = defaultdict(list)
    body_ok = set()
    for f in sorted(data_dir.glob("[0-9]*_[0-9]*.csv")):
        i, j = map(int, f.stem.split("_"))
        p = sections(f.read_text(encoding="utf-8"))
        if "END" not in p:
            sys.exit(f"{f.name}: Prüfmarke #END fehlt, Export unvollständig")
        for r in rows(p["BUILDINGS"]):
            area[r["bldg_uuid"]] = float(r["area"])
            cell_of[r["bldg_uuid"]] = (i, j)
        for r in rows(p["BRIDGE"]):
            ri = float(r["relative_intersection"]) if r["relative_intersection"] else 0.0
            bridge[r["bldg_uuid"]].append((int(r["body"]), r["match_type"], ri))
        for r in rows(p["OVERLAP"]):
            overlap[r["bldg_uuid"]].append((int(r["body"]), float(r["overlap_area"])))
        for line in p["BODIES"].splitlines()[1:]:
            body_ok.add(int(line.split(",", 1)[0]))

    # bridge
    claims = defaultdict(list)  # body -> [(ri, uuid)]
    for uuid, cands in bridge.items():
        cands = [c for c in cands if c[0] in body_ok]
        one = [c for c in cands if c[1] == "1:1"]
        for body, _mt, ri in (one or cands):
            claims[body].append((ri, uuid))
    owner = {body: max(cl)[1] for body, cl in claims.items()}
    bodies_of = defaultdict(list)
    for body, uuid in owner.items():
        bodies_of[uuid].append(body)
    tier = {u: "bridge" for u in bodies_of}

    # spatial
    best = {}
    for uuid, ov in overlap.items():
        if uuid in tier:
            continue
        free = [(a / max(area[uuid], 1.0), body) for body, a in ov if body in body_ok and body not in owner]
        if free:
            f, body = max(free)
            if f >= SPATIAL_MIN:
                best[uuid] = (f, body)
    by_body = defaultdict(list)
    for uuid, (f, body) in best.items():
        by_body[body].append((f, uuid))
    for body, cl in by_body.items():
        uuid = max(cl)[1]
        owner[body] = uuid
        bodies_of[uuid].append(body)
        tier[uuid] = "spatial"

    # covered / estimated
    covered_by = {}
    for uuid in area:
        if uuid in tier:
            continue
        taken = [(a, owner[body]) for body, a in overlap.get(uuid, []) if body in owner and owner[body] != uuid]
        if taken and sum(a for a, _ in taken) / max(area[uuid], 1.0) >= COVERED_MIN:
            tier[uuid] = "covered"
            covered_by[uuid] = max(taken)[1]
        else:
            tier[uuid] = "estimated"

    lost = sum(1 for u in bridge if tier.get(u) == "estimated" and u not in overlap)
    return cell_of, tier, dict(bodies_of), covered_by, lost


# ---------------------------------------------------------------------------------------------- Schritt 2

def build_cell(args):
    path, out, assignment = args
    i, j = map(int, Path(path).stem.split("_"))
    p = sections(Path(path).read_text(encoding="utf-8"))
    body_surf = defaultdict(list)
    for r in rows(p["BODIES"]):
        g = json.loads(r["geom"])
        for rings in ([g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]):
            body_surf[int(r["body"])].append((int(r["cls"]), rings))

    fine, coarse = defaultdict(MeshBuilder), MeshBuilder()
    attrs, overview_items = {}, []
    for b in rows(p["BUILDINGS"]):
        uuid = b["bldg_uuid"]
        t, bodies, cb = assignment[uuid]
        fp = json.loads(b["footprint"])
        ring = np.asarray(fp["coordinates"][0] if fp["type"] == "Polygon" else fp["coordinates"][0][0])
        fi = min(max(math.floor(ring[:, 0].mean() / FINE), i * 4), i * 4 + 3)
        fj = min(max(math.floor(ring[:, 1].mean() / FINE), j * 4), j * 4 + 3)
        est = float(b["est_height"] or 9)
        height = est
        if t in ("bridge", "spatial"):
            surf = [s for body in bodies for s in body_surf.get(body, [])]
            zs = [pt[2] for _c, rings in surf for r in rings for pt in r]
            base = min(zs) if zs else 0.0
            if zs and fine[(fi, fj)].add_building(uuid, t, surf, base):
                height = max(3.0, max(zs) - base)
            else:
                t = "estimated"
        if t == "estimated":
            fine[(fi, fj)].add_building(uuid, t, extrude(fp, est), 0.0)
        if float(b["area"]) >= COARSE_MIN_AREA:
            coarse.add_building(uuid, t, extrude(json.loads(b["footprint_simple"]), height), 0.0)
        if float(b["area"]) >= OVERVIEW_MIN_AREA:
            overview_items.append((uuid, t, b["footprint_overview"], height))

        def num(k):
            try:
                return round(float(b[k]), 1)
            except (TypeError, ValueError):
                return None

        addr = " ".join(x for x in [b.get("street_name"), b.get("house_number")] if x)
        a = {"t": t, "hs": num("heat_spec"), "hk": num("heat_kwh"), "ha": num("heated_area"),
             "y": b.get("year") or None, "u": b.get("usage") or None, "a": addr or None}
        if cb:
            a["cb"] = cb
        attrs[uuid] = a

    if not attrs:
        return None
    out = Path(out)
    attrs_name = f"attrs/{i}_{j}.json"
    (out / attrs_name).write_text(json.dumps(attrs, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    stats = Counter(a["t"] for a in attrs.values())
    kids = []
    for (fi, fj), mb in fine.items():
        if not mb.uuids:
            continue
        name = f"fine/{fi}_{fj}.glb"
        tr, hmin, hmax, nv, nt = write_glb(mb, out / name, ((fi + 0.5) * FINE, (fj + 0.5) * FINE))
        stats["bytes_fine"] += (out / name).stat().st_size
        stats["vertices_fine"] += nv
        stats["triangles_fine"] += nt
        kids.append({"boundingVolume": {"region": region_of(fi * FINE, fj * FINE, FINE, hmin, hmax)},
                     "geometricError": GEOMETRIC_ERROR["fine"], "transform": tr,
                     "content": {"uri": name}, "extras": {"attrs": [attrs_name]}})
    tile = {"geometricError": GEOMETRIC_ERROR["coarse"], "refine": "REPLACE",
            "extras": {"attrs": [attrs_name]}, "children": kids}
    hmin = hmax = 0.0
    if coarse.uuids:
        name = f"coarse/{i}_{j}.glb"
        tr, hmin, hmax, _nv, _nt = write_glb(coarse, out / name, ((i + 0.5) * CELL, (j + 0.5) * CELL))
        stats["bytes_coarse"] += (out / name).stat().st_size
        tile.update({"transform": tr, "content": {"uri": name}})
    else:
        tile["transform"] = cols(np.eye(4))
    hmax = max([hmax] + [k["boundingVolume"]["region"][5] for k in kids])
    tile["boundingVolume"] = {"region": region_of(i * CELL, j * CELL, CELL, min(hmin, 0.0), hmax)}
    stats["bytes_attrs"] += (out / attrs_name).stat().st_size
    return (i, j), tile, overview_items, dict(stats)


def relative(children, parent_abs):
    """Kinder-Transforms sind absolut gebaut; 3D Tiles erwartet sie relativ zum Elternteil."""
    inv = np.linalg.inv(mat(parent_abs))
    out = []
    for c in children:
        c = dict(c)
        absolute = c["transform"]
        c["transform"] = cols(inv @ mat(absolute))
        if c.get("children"):
            c["children"] = relative(c["children"], absolute)
        out.append(c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    t0 = time.time()
    data, out = Path(args.data_dir), Path(args.out_dir)
    for sub in ("fine", "coarse", "overview", "attrs"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    cell_of, tier, bodies_of, covered_by, lost = assign(data)
    t_assign = time.time() - t0
    print(f"Zuordnung: {len(tier)} Gebäude, {dict(Counter(tier.values()))}, {t_assign:.1f} s", flush=True)

    by_cell = defaultdict(dict)
    for uuid, c in cell_of.items():
        by_cell[c][uuid] = (tier[uuid], bodies_of.get(uuid, []), covered_by.get(uuid))
    jobs = [(str(data / f"{i}_{j}.csv"), str(out), by_cell[(i, j)]) for (i, j) in sorted(by_cell)]
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        results = [r for r in pool.map(build_cell, jobs, chunksize=1) if r]
    t_cells = time.time() - t0 - t_assign

    # Übersichtsstufe
    stats = Counter()
    groups = defaultdict(list)
    for (i, j), tile, items, st in results:
        stats.update(st)
        groups[(i // OVERVIEW_CELLS, j // OVERVIEW_CELLS)].append(((i, j), tile, items))
    size = CELL * OVERVIEW_CELLS
    top = []
    for (oi, oj), members in sorted(groups.items()):
        mb = MeshBuilder()
        for _c, _t, items in members:
            for uuid, t, fp, h in items:
                mb.add_building(uuid, t, extrude(json.loads(fp), h), 0.0)
        children = [tile for _c, tile, _i in members]
        hmax = max(c["boundingVolume"]["region"][5] for c in children)
        # Die Übersicht braucht nur Stufe und Heizwert zum Einfärben, nicht die vollen Zell-Attribute
        # (für ganz Berlin wären das ~100 MB). Der Viewer überschreibt damit keine vollen Einträge.
        cell_attrs = {}
        for c in children:
            cell_attrs.update(json.loads((out / c["extras"]["attrs"][0]).read_text(encoding="utf-8")))
        slim = {u: {"t": cell_attrs[u]["t"], "hs": cell_attrs[u]["hs"]} for u in mb.uuids}
        attrs_name = f"attrs/overview_{oi}_{oj}.json"
        (out / attrs_name).write_text(json.dumps(slim, separators=(",", ":")), encoding="utf-8")
        stats["bytes_attrs_overview"] += (out / attrs_name).stat().st_size
        node = {"boundingVolume": {"region": region_of(oi * size, oj * size, size, 0.0, hmax)},
                "geometricError": GEOMETRIC_ERROR["overview"], "refine": "REPLACE",
                "extras": {"attrs": [attrs_name]}}
        if mb.uuids:
            name = f"overview/{oi}_{oj}.glb"
            tr, *_ = write_glb(mb, out / name, ((oi + 0.5) * size, (oj + 0.5) * size))
            stats["bytes_overview"] += (out / name).stat().st_size
            node.update({"transform": tr, "content": {"uri": name}, "children": relative(children, tr)})
        else:
            node["children"] = relative(children, cols(np.eye(4)))
        top.append(node)

    regs = np.array([n["boundingVolume"]["region"] for n in top])
    root_reg = [regs[:, 0].min(), regs[:, 1].min(), regs[:, 2].max(), regs[:, 3].max(),
                regs[:, 4].min(), regs[:, 5].max()]
    tileset = {
        "asset": {"version": "1.1", "generator": "enpageo tiles/build_region.py"},
        "geometricError": GEOMETRIC_ERROR["root"] * 2,
        "root": {"boundingVolume": {"region": root_reg}, "geometricError": GEOMETRIC_ERROR["root"],
                 "refine": "REPLACE", "children": top},
    }
    (out / "tileset.json").write_text(json.dumps(tileset, separators=(",", ":")), encoding="utf-8")

    report = {
        "buildings": len(tier), "tiers": dict(Counter(tier.values())),
        "estimated_after_lost_bridge_claims": lost,
        "cells": len(results), "overview_tiles": len(top),
        "fine_tiles": sum(len(t["children"]) for _c, t, _i, _s in results),
        **{k: v for k, v in stats.items() if k.startswith(("bytes", "vertices", "triangles"))},
        "seconds_assign": round(t_assign, 1), "seconds_cells": round(t_cells, 1),
        "seconds_total": round(time.time() - t0, 1),
    }
    (out / "build_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
