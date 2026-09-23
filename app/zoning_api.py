"""API for the zoning page (/de/zoning).

Reads only the `zoning` schema. That is deliberate: the schema carries copies of
everything the page needs, so the page runs on the VPS, which does not have the
266 GB EnergyMap database behind it.

Every value the page shows carries a source and a certainty, because that distinction
is the point of the tool:

  gemessen    ALKIS / CityGML          certainty 1.0
  berechnet   pure geometry, our own   certainty 1.0  (deterministic)
  Pipeline    EnergyMap-derived        certainty 0.8  (modelled, not observed)
  Bildmodell  facade question          certainty = the answer distribution's mode
  korrigiert  a person overrode it     certainty 1.0
  -           not collected yet        certainty null
"""

from __future__ import annotations

import json

from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .database import get_db

router = APIRouter(prefix="/api/zoning", tags=["zoning"])

PIPELINE_CERTAINTY = 0.8

# Facade crops live beside the zoning package, never anywhere else. Every path read
# from the database is checked against this root before it is served, so a bad row
# cannot turn the endpoint into a file browser.
IMAGE_ROOT = (Path(__file__).resolve().parents[2] / "zoning" / "images").resolve()

# Plain-language source names for the height levels in zoning.certainty_level, so the
# dashboard says where a height came from instead of claiming it was simply computed.
# Residential in the sense German building statistics use: a building counts as a
# Wohngebäude when it mainly serves living, and halls of residence and holiday homes
# belong there too. The zoning categories map onto that as follows.
RESIDENTIAL = ("wohnen", "wohnen_efh", "wohnheim", "freizeitwohnen")

# Surface classes in this 3DCityDB. Read from citydb.objectclass rather than assumed:
# the numbering here is shifted against the one the 3DCityDB documentation shows, so
# 33 is the roof and 34 the wall, not the other way round.
SURFACE_KIND = {33: "roof", 34: "wall", 35: "ground", 36: "wall"}

HEIGHT_SOURCE = {
    "hoehe_lod2_traufe": "LoD2-Körper (Traufhöhe)",
    "hoehe_lod2_teilkoerper": "LoD2-Teilkörper, gemittelt",
    "hoehe_alkis_gemessen": "ALKIS, gemessen",
    "hoehe_geschosse_alkis": "ALKIS-Geschosse × Geschosshöhe der Kategorie",
    "hoehe_pipeline_lichte": "EnergyMap-Pipeline (lichte Höhe)",
    "hoehe_widerspruch": "Quellen widersprüchlich",
}


def _fc(rows, geom_key="geom", props=lambda r: {}):
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": json.loads(getattr(r, geom_key)), "properties": props(r)}
            for r in rows
            if getattr(r, geom_key)
        ],
    }


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT a.area_id, a.name,
               (SELECT count(*) FROM zoning.candidate WHERE area_id = a.area_id) AS candidates,
               (SELECT count(*) FROM zoning.candidate c
                  JOIN zoning.building_feature f USING (bldg_uuid)
                 WHERE c.area_id = a.area_id
                   AND f.usage_category = ANY(:res)) AS residential,
               (SELECT count(*) FROM zoning.candidate c
                  JOIN zoning.building_feature f USING (bldg_uuid)
                 WHERE c.area_id = a.area_id
                   AND (f.usage_category IS NULL
                        OR f.usage_category <> ALL(:res))) AS non_residential,
               (SELECT count(DISTINCT zs.bldg_uuid) FROM zoning.zone_set zs
                  JOIN zoning.candidate c USING (bldg_uuid)
                 WHERE c.area_id = a.area_id) AS zoned,
               (SELECT count(*) FROM zoning.building_feature f
                  JOIN zoning.candidate c USING (bldg_uuid)
                 WHERE c.area_id = a.area_id AND f.chiller_count > 0) AS with_chillers,
               ST_AsGeoJSON(ST_Transform(a.geom, 4326), 6) AS geom
        FROM zoning.area a ORDER BY a.area_id LIMIT 1
    """), {"res": list(RESIDENTIAL)}).first()
    if row is None:
        raise HTTPException(404, "no work area")
    return {
        "area_id": row.area_id,
        "name": row.name,
        "candidates": row.candidates,
        "residential": row.residential,
        "non_residential": row.non_residential,
        "zoned": row.zoned,
        "with_chillers": row.with_chillers,
        "outline": json.loads(row.geom) if row.geom else None,
    }


@router.get("/context")
def context(db: Session = Depends(get_db)):
    """Every heated building of the district, split residential / non-residential.

    They are all candidates -- the tool covers the whole heated stock, not only the
    non-residential part -- so the old is_candidate flag no longer distinguishes
    anything and the split that matters is by use.

    Simplified only lightly. An earlier version used a 2 m tolerance and 5 decimals to
    save payload, which moved edges by up to 3 m -- visibly off against the selected
    building's own outline, which is served unsimplified at 7 decimals. Both come from
    the same ALKIS geometry and differ by 0,20 m in the database, so any larger gap on
    screen was made here, not in the data. 0,5 m and 6 decimals costs 2 MB more and
    lines up.
    """
    rows = db.execute(text("""
        SELECT (f.usage_category = ANY(:res)) AS residential,
               ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(g.geom, 0.5), 4326), 6) AS geom
        FROM zoning.context_geom g
        LEFT JOIN zoning.building_feature f USING (bldg_uuid)
    """), {"res": list(RESIDENTIAL)}).all()
    return _fc(rows, props=lambda r: {"w": 1 if r.residential else 0})


@router.get("/tiles")
def tiles(db: Session = Depends(get_db)):
    """The HOSTRADA 1 km cells over the work area.

    Cells are stored as they come out of the NetCDF coordinate axes, so the outlines
    drawn here are the actual weather cells, not a reconstructed grid. `candidates`
    says how many of the tool's buildings fall into each cell -- a cell with none is
    drawn fainter, because no weather is ever read for it.
    """
    rows = db.execute(text("""
        SELECT tile_id, n_buildings, n_candidates,
               ST_AsGeoJSON(ST_Transform(geom, 4326), 6) AS geom
        FROM zoning.hostrada_tile ORDER BY tile_id
    """)).all()
    return _fc(rows, props=lambda r: {
        "tile_id": r.tile_id, "buildings": r.n_buildings, "candidates": r.n_candidates,
    })


@router.get("/scan")
def scan(index: int = Query(0, ge=0), db: Session = Depends(get_db)):
    """The building at scan position `index`.

    Order is rows north to south, west to east within a row -- computed once into
    zoning.candidate.scan_index, so the page never has to sort 3.500 buildings.
    """
    row = db.execute(text("""
        SELECT c.bldg_uuid, c.scan_index, f.*,
               ST_AsGeoJSON(ST_Transform(g.geom, 4326), 7) AS geom,
               ST_X(ST_Transform(g.centroid, 4326)) AS lon,
               ST_Y(ST_Transform(g.centroid, 4326)) AS lat,
               (f.usage_category = ANY(:res)) AS residential,
               (SELECT count(*) FROM zoning.candidate WHERE area_id = c.area_id
                  AND scan_index IS NOT NULL) AS total
        FROM zoning.candidate c
        JOIN zoning.building_feature f USING (bldg_uuid)
        JOIN zoning.building_geom g USING (bldg_uuid)
        WHERE c.scan_index IS NOT NULL AND c.scan_index > :i
        ORDER BY c.scan_index LIMIT 1
    """), {"i": index, "res": list(RESIDENTIAL)}).first()
    if row is None:
        raise HTTPException(404, "scan position beyond the last building")

    return {
        "bldg_uuid": row.bldg_uuid,
        "scan_index": row.scan_index,
        "total": row.total,
        "geometry": json.loads(row.geom),
        "centre": [row.lon, row.lat],
        # the resolved eaves height, not the old storeys x clear-room-height value
        "height_m": float(row.height_best_m or row.height_m or 0) or None,
        "storeys": float(row.storey_number) if row.storey_number else None,
        "usage": row.usage_primary,
        "residential": bool(row.residential),
        "chillers": row.chiller_count,
    }


def _param(name, value, source, certainty, unit=None):
    return {"name": name, "value": value, "unit": unit, "source": source, "certainty": certainty}


@router.get("/building/{uuid}/parameters")
def parameters(uuid: str, db: Session = Depends(get_db)):
    f = db.execute(text("""
        SELECT bf.*, bh.ridge_m
        FROM zoning.building_feature bf
        LEFT JOIN zoning.building_height bh USING (bldg_uuid)
        WHERE bf.bldg_uuid = :u"""), {"u": uuid}).mappings().first()
    if f is None:
        raise HTTPException(404, "unknown building")

    out = [
        _param("Gebäude-UUID", uuid, "CityGML", 1.0),
        _param("EnergyMap Gebäudetyp", f["usage_primary"], "ALKIS", 1.0),
        _param("ALKIS-Schlüssel", f["alkis_code"], "ALKIS", 1.0),
        _param("Grundfläche", round(f["footprint_area"] or 0), "ALKIS", 1.0, "m²"),
        _param("Baujahr", f["year_of_construction"], "EnergyMap-Pipeline", PIPELINE_CERTAINTY),
        _param("Geschosse", f["storey_number"], "EnergyMap-Pipeline", PIPELINE_CERTAINTY),
        _param("Lichte Raumhöhe", round(f["storey_height_avg"] or 0, 2),
               "EnergyMap-Pipeline", PIPELINE_CERTAINTY, "m"),
        _param("Gebäudehöhe (Traufe)", round(f["height_best_m"] or 0, 1),
               HEIGHT_SOURCE.get(f["height_code"], "berechnet"),
               float(f["height_confidence"] or 0), "m"),
        _param("Bruttogeschosshöhe", round(f["storey_height_gross_m"] or 0, 2),
               HEIGHT_SOURCE.get(f["height_code"], "berechnet"),
               float(f["height_confidence"] or 0), "m"),
        # the 3D panel draws up to the ridge, so the ridge has to be readable too --
        # otherwise the drawing looks taller than every number next to it
        _param("Firsthöhe", round(f["ridge_m"], 1) if f["ridge_m"] else None,
               "LoD2-Körper", 0.9, "m"),
        _param("Beheizte Fläche", round(f["heated_area"] or 0), "EnergyMap-Pipeline",
               PIPELINE_CERTAINTY, "m²"),
        _param("Gebäudetiefe", round(f["depth_m"] or 0, 1), "berechnet", 1.0, "m"),
        _param("Tiefenklasse", f["depth_class"], "berechnet", 1.0),
        _param("Kern möglich", "ja" if f["core_feasible"] else "nein", "berechnet", 1.0),
        _param("Freie Fassade", round(f["free_edge_m"] or 0, 1), "berechnet", 1.0, "m"),
        _param("Brandwände", round(f["shared_edge_m"] or 0, 1), "berechnet", 1.0, "m"),
        _param("Nachbargebäude", f["neighbour_count"], "berechnet", 1.0),
        _param("Dachkältemaschinen", f["chiller_count"], "Luftbildauswertung", 0.93),
    ]
    if f["chiller_count"]:
        out.append(_param("Fläche Kältemaschinen", round(f["chiller_area_m2"] or 0, 1),
                          "Luftbildauswertung", 0.93, "m²"))

    facades = db.execute(text("""
        SELECT orientation, is_shared, round(length_m::numeric, 1) len
        FROM zoning.facade WHERE bldg_uuid = :u ORDER BY is_shared, len DESC
    """), {"u": uuid}).all()

    return {
        "parameters": out,
        "facades": [{"orientation": r.orientation, "shared": r.is_shared, "length_m": float(r.len)}
                    for r in facades],
    }


@router.get("/building/{uuid}/answers")
def answers(uuid: str, db: Session = Depends(get_db)):
    """Facade questions: the catalogue with whatever answer exists, or none yet."""
    rows = db.execute(text("""
        SELECT q.question_id, q.label_de, q.options, q.sort_order,
               a.value_model, a.value_human, a.value_final, a.confidence,
               a.distribution, a.model_name, a.n_samples, a.cross_check, a.image_id
        FROM zoning.question q
        LEFT JOIN zoning.answer a ON a.question_id = q.question_id AND a.bldg_uuid = :u
        WHERE q.active ORDER BY q.sort_order
    """), {"u": uuid}).all()

    out = []
    for r in rows:
        if r.value_human is not None:
            source, certainty = "korrigiert", 1.0
        elif r.value_model is not None:
            source, certainty = f"Bildmodell ({r.model_name})", r.confidence
        else:
            source, certainty = None, None
        out.append({
            "question_id": r.question_id,
            "label": r.label_de,
            "options": r.options,
            "value": r.value_final,
            "source": source,
            "certainty": certainty,
            "distribution": r.distribution,
            "n_samples": r.n_samples,
            "cross_check": r.cross_check,
            "image_id": r.image_id,
        })
    return {"answers": out}


@router.get("/building/{uuid}/facade")
def facade(uuid: str, db: Session = Depends(get_db)):
    """The chosen street view and how it was taken.

    The capture geometry is the point of this endpoint. A Mapillary panorama is a
    picture of a street, not of an address -- what makes it a facade view is the
    re-projection, and whether that worked can only be judged next to the numbers it
    was computed from: how far the camera stood, in which direction it was turned, how
    wide a field of view was needed to fit the building in.
    """
    r = db.execute(text("""
        SELECT i.image_id, i.dist_m, i.azimuth_deg, i.compass_deg, i.hfov_deg,
               i.vfov_deg, i.pitch_deg, i.bldg_height_m, i.captured_at, i.creator,
               i.src_w, i.src_h, i.crop_path
        FROM zoning.facade_image i WHERE i.bldg_uuid = :u
    """), {"u": uuid}).mappings().first()
    if r is None or not r["crop_path"]:
        return {"has_image": False}

    compass = {0: "N", 45: "NO", 90: "O", 135: "SO",
               180: "S", 225: "SW", 270: "W", 315: "NW"}
    az = float(r["azimuth_deg"] or 0)
    facing = compass[min(compass, key=lambda k: min(abs(az - k), 360 - abs(az - k)))]

    return {
        "has_image": True,
        "url": f"/api/zoning/building/{uuid}/facade.jpg",
        "image_id": str(r["image_id"]),
        "dist_m": round(float(r["dist_m"]), 1) if r["dist_m"] is not None else None,
        "azimuth_deg": round(az),
        "facing": facing,
        "hfov_deg": round(float(r["hfov_deg"] or 0)),
        "vfov_deg": round(float(r["vfov_deg"] or 0)),
        "pitch_deg": round(float(r["pitch_deg"] or 0)),
        "height_m": round(float(r["bldg_height_m"] or 0), 1),
        "captured_at": r["captured_at"].date().isoformat() if r["captured_at"] else None,
        "creator": r["creator"],
        "source": "Mapillary, auf die Fassade umprojiziert",
        "licence": "CC-BY-SA 4.0",
        "certainty": 0.9,
    }


@router.get("/building/{uuid}/facade.jpg")
def facade_jpg(uuid: str, db: Session = Depends(get_db)):
    path = db.execute(text("SELECT crop_path FROM zoning.facade_image "
                           "WHERE bldg_uuid = :u"), {"u": uuid}).scalar()
    if not path:
        raise HTTPException(404, "no facade image for this building")
    f = Path(path).resolve()
    if not f.is_file() or IMAGE_ROOT not in f.parents:
        raise HTTPException(404, "image file missing")
    return FileResponse(f, media_type="image/jpeg")


@router.post("/building/{uuid}/answer")
def correct_answer(uuid: str, question_id: str = Body(..., embed=True),
                   value: str | None = Body(None, embed=True),
                   by: str = Body("dashboard", embed=True),
                   db: Session = Depends(get_db)):
    """Write a human correction, or clear one by sending value = null.

    The model answer is never touched. `value_final` prefers the human value, so a
    correction wins without erasing what the model said -- the disagreement stays
    visible and is worth keeping.
    """
    ok = db.execute(text("SELECT options FROM zoning.question "
                         "WHERE question_id = :q AND active"), {"q": question_id}).first()
    if ok is None:
        raise HTTPException(404, "unknown question")
    if value is not None and value not in (ok.options or []):
        raise HTTPException(400, f"'{value}' is not an option for {question_id}")

    db.execute(text("""
        INSERT INTO zoning.answer (bldg_uuid, question_id, value_human, corrected_at,
                                   corrected_by)
        VALUES (:u, :q, :v, now(), :b)
        ON CONFLICT (bldg_uuid, question_id) DO UPDATE SET
          value_human = EXCLUDED.value_human,
          corrected_at = CASE WHEN EXCLUDED.value_human IS NULL THEN NULL ELSE now() END,
          corrected_by = CASE WHEN EXCLUDED.value_human IS NULL THEN NULL ELSE EXCLUDED.corrected_by END
    """), {"u": uuid, "q": question_id, "v": value, "b": by})
    db.commit()
    return {"ok": True, "question_id": question_id, "value": value}


@router.get("/building/{uuid}/lod2")
def lod2(uuid: str, db: Session = Depends(get_db)):
    """The real LoD2 solid from the 3D city database -- walls and roof, not an extrusion.

    An extruded footprint is a flat lie about anything with a pitched roof, and pitch
    and orientation are exactly what decides how much sun a roof takes. The geometry is
    already here: `citydb` holds 1.016.484 buildings with 34,1 million surfaces, and
    `zoning.building_citydb` is the bridge from our 2D building to its parts -- matched
    on overlapping ground surfaces rather than taken from the pipeline's link table,
    which points at the wrong solid for about 7 % of the buildings.

    Heights come out of CityGML as metres above sea level (~34 m at ground level in
    Berlin). They are shifted so the lowest point of the building sits at zero, because
    the map draws relative to its own terrain.
    """
    rows = db.execute(text("""
        SELECT ts.objectclass_id AS cls,
               ST_ZMin(sg.geometry) AS zmin, ST_ZMax(sg.geometry) AS zmax,
               -- A wall standing on another building's outline is a party wall: no
               -- windows, no sun, and the zoning solver has to know. The neighbours are
               -- taken from the city model's own ground surfaces, not from ALKIS: ALKIS
               -- files one physical block under several keys -- thirteen of them overlap
               -- this building alone -- so "any other ALKIS outline nearby" marks every
               -- wall, including the street facade.
               EXISTS (SELECT 1 FROM zoning.citydb_ground o
                        WHERE o.citydb_id NOT IN (SELECT citydb_id
                                                    FROM zoning.building_citydb
                                                   WHERE bldg_uuid = :u)
                          AND o.geom && ST_Expand(ST_Force2D(sg.geometry), 0.6)
                          AND ST_DWithin(o.geom, ST_Force2D(sg.geometry), 0.5)) AS shared,
               ST_AsGeoJSON(ST_Transform(sg.geometry, 4326), 7) AS geom
        FROM zoning.building_citydb rel
        JOIN citydb.thematic_surface ts ON ts.building_id = rel.citydb_id
        JOIN citydb.surface_geometry sg ON sg.root_id = ts.lod2_multi_surface_id
        WHERE rel.bldg_uuid = :u
          AND ts.objectclass_id = ANY(:cls)
          AND sg.geometry IS NOT NULL
    """), {"u": uuid, "cls": list(SURFACE_KIND)}).all()
    if not rows:
        return {"has_lod2": False, "surfaces": _fc([], props=lambda r: {})}

    base = min(float(r.zmin) for r in rows)
    top = max(float(r.zmax) for r in rows)
    feats = []
    for r in rows:
        g = json.loads(r.geom)
        _shift(g["coordinates"], base)
        feats.append({"type": "Feature", "geometry": g,
                      "properties": {"kind": SURFACE_KIND[r.cls],
                                     "shared": bool(r.shared)}})
    kinds: dict[str, int] = {}
    for f in feats:
        k = f["properties"]["kind"]
        if k == "wall" and f["properties"]["shared"]:
            k = "shared"
        kinds[k] = kinds.get(k, 0) + 1
    # What the drawn body is, against what the parameter table states. They are not
    # the same quantity: the solid reaches the RIDGE, the stated height is the EAVES.
    # Over the district the drawn body is a median 1,5 to 3,3 m taller, which is simply
    # the roof. A much larger gap means something else -- usually that the 2D outline
    # and the 3D solid describe different buildings, which happens for about 5 % of them.
    ref = db.execute(text("""
        SELECT bf.height_best_m, bf.height_code, bh.ridge_m
        FROM zoning.building_feature bf
        LEFT JOIN zoning.building_height bh USING (bldg_uuid)
        WHERE bf.bldg_uuid = :u"""), {"u": uuid}).mappings().first()
    model_h = round(top - base, 1)
    # Compare like with like: the drawn solid reaches the ridge, so it is checked
    # against the ridge height, not against the eaves the table shows. A drawing that
    # is a few metres taller than the eaves is simply a roof, not an error.
    ridge = float(ref["ridge_m"]) if ref and ref["ridge_m"] else None
    off = abs(model_h - ridge) > 2 if ridge is not None else False
    # And if the height resolution rejected the LoD2 link as implausible, the solid
    # shown is very likely a different building -- that is the real failure case.
    rejected = bool(ref and ref["height_code"] not in
                    ("hoehe_lod2_traufe", "hoehe_lod2_teilkoerper"))
    return {
        "has_lod2": True,
        "base_m": round(base, 2),
        "model_height_m": model_h,
        "stated_height_m": round(float(ref["height_best_m"]), 1)
                           if ref and ref["height_best_m"] else None,
        "ridge_m": round(ridge, 1) if ridge is not None else None,
        "suspect": bool(off or rejected),
        "counts": kinds,
        "surfaces": {"type": "FeatureCollection", "features": feats},
    }


def _shift(coords, base: float) -> None:
    """Subtract the building's own ground level from every z, in place."""
    if coords and isinstance(coords[0], (int, float)):
        if len(coords) > 2:
            coords[2] = round(coords[2] - base, 3)
        return
    for c in coords:
        _shift(c, base)


@router.get("/chillers")
def chillers(db: Session = Depends(get_db)):
    """Rooftop cooling equipment detected from aerial imagery -- the validation data.

    Not our own product: it comes from `core_building."Chillers"`, 5.436 outlines for
    Berlin, of which 643 sit on buildings in this work area. Drawn on the map because
    a zoning prediction for a building that demonstrably has a chiller on its roof is
    worth more than one for a building that may not be cooled at all.

    The real outlines, not centroids: a cooling tower is a different shape from a
    packaged chiller, and the class in the table refers to that shape. At district zoom
    a 9 m2 unit is well under a pixel, so the layer only becomes readable zoomed in --
    which is where the shape is the point.
    """
    rows = db.execute(text("""
        SELECT ch.class AS cls, round(ch.shape_area::numeric, 1) AS area,
               round(ch.confidence::numeric, 1) AS conf,
               ST_AsGeoJSON(ST_Transform(ch.geom, 4326), 7) AS geom
        FROM core_building."Chillers" ch
        JOIN zoning.area a ON ST_Intersects(ch.geom, a.geom)
    """)).all()
    return _fc(rows, props=lambda r: {
        # confidence is missing on part of the detections; a null says "unknown",
        # which is not the same as a low score and must not be turned into one
        "class": r.cls, "area_m2": float(r.area) if r.area is not None else None,
        "confidence": float(r.conf) if r.conf is not None else None,
    })


@router.get("/building-at")
def building_at(lon: float = Query(...), lat: float = Query(...),
                db: Session = Depends(get_db)):
    """The candidate building under a map click, with its position in the scan order."""
    row = db.execute(text("""
        WITH p AS (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                                       25833) g)
        SELECT c.bldg_uuid, c.scan_index,
               round(ST_Distance(bg.geom, p.g)::numeric, 1) AS dist
        FROM zoning.candidate c
        JOIN zoning.building_geom bg USING (bldg_uuid), p
        WHERE c.area_id = 2 AND ST_DWithin(bg.geom, p.g, 30)
        ORDER BY ST_Distance(bg.geom, p.g) LIMIT 1
    """), {"lon": lon, "lat": lat}).first()
    if row is None:
        raise HTTPException(404, "no candidate building near this point")
    return {"bldg_uuid": row.bldg_uuid, "scan_index": row.scan_index,
            "dist_m": float(row.dist)}


@router.get("/building/{uuid}/zones")
def zones(uuid: str, sample: int = Query(0, ge=-1), storey: int | None = Query(None),
          storey_type: str = Query("ground"), db: Session = Depends(get_db)):
    """One sample's zone layout for one level, as polygons to draw.

    `sample = -1` is the measured set read from a room book, not a draw from the
    ensemble. Levels can be addressed two ways because the two kinds of set number
    them differently: a drawn set has storey TYPES (ground / standard / attic, one
    row standing for several floors), a measured set has real storey NUMBERS from
    the room numbering. `storey` wins when given.
    """
    rows = db.execute(text("""
        SELECT z.zone_kind, z.orientation, z.usage_zone, z.has_daylight,
               round(z.area_m2::numeric, 1) area_m2, z.storeys_represented,
               z.storey, z.source, z.room_count, z.area_code, z.usage_code,
               round(z.area_confidence::numeric, 2) area_confidence,
               ST_AsGeoJSON(ST_Transform(z.geom, 4326), 7) AS geom
        FROM zoning.zone z
        JOIN zoning.zone_set zs USING (zone_set_id)
        WHERE zs.bldg_uuid = :u AND zs.sample_index = :s AND {level}
        ORDER BY z.area_m2 DESC
    """.format(level="z.storey = :storey" if storey is not None
                     else "z.storey_type = :st")),
        {"u": uuid, "s": sample, "st": storey_type, "storey": storey}).all()

    meta = db.execute(text("""
        SELECT count(*) FILTER (WHERE sample_index >= 0) AS n_samples,
               max(storey_count) AS storeys,
               bool_or(sample_index = -1) AS has_measured,
               bool_and(COALESCE(complete, true)) AS complete
        FROM zoning.zone_set WHERE bldg_uuid = :u
    """), {"u": uuid}).first()

    types = db.execute(text("""
        SELECT DISTINCT z.storey_type, z.storeys_represented
        FROM zoning.zone z JOIN zoning.zone_set zs USING (zone_set_id)
        WHERE zs.bldg_uuid = :u AND zs.sample_index = :s ORDER BY 1
    """), {"u": uuid, "s": sample}).all()

    levels = db.execute(text("""
        SELECT z.storey, count(*) n, round(sum(z.area_m2)::numeric) area
        FROM zoning.zone z JOIN zoning.zone_set zs USING (zone_set_id)
        WHERE zs.bldg_uuid = :u AND zs.sample_index = :s
        GROUP BY 1 ORDER BY 1
    """), {"u": uuid, "s": sample}).all()

    return {
        "n_samples": meta.n_samples if meta else 0,
        "storeys": meta.storeys if meta else None,
        "has_measured": bool(meta.has_measured) if meta else False,
        "complete": bool(meta.complete) if meta else True,
        "storey_types": [{"type": t.storey_type, "represents": t.storeys_represented}
                         for t in types],
        "levels": [{"storey": l.storey, "zones": l.n, "area_m2": float(l.area)}
                   for l in levels],
        "zones": _fc(rows, props=lambda r: {
            "kind": r.zone_kind, "orientation": r.orientation, "usage": r.usage_zone,
            "daylight": r.has_daylight, "area_m2": float(r.area_m2),
            "storey": r.storey, "source": r.source, "rooms": r.room_count,
            "area_code": r.area_code, "usage_code": r.usage_code,
            "certainty": float(r.area_confidence) if r.area_confidence is not None else None,
        }),
    }


@router.get("/building/{uuid}/zone-summary")
def zone_summary(uuid: str, storey_type: str = Query("ground"), db: Session = Depends(get_db)):
    """Per usage zone across the whole ensemble: area spread and how often it appears.

    This is the probabilistic statement the drawing alone cannot make. `certainty` is
    the share of samples in which the zone occurs at all -- a zone present in every
    sample is a safe call, one present in three of ten is not.
    """
    rows = db.execute(text("""
        WITH per_sample AS (
            SELECT zs.sample_index, z.usage_zone, sum(z.area_m2) area
            FROM zoning.zone z JOIN zoning.zone_set zs USING (zone_set_id)
            WHERE zs.bldg_uuid = :u AND z.storey_type = :st
            GROUP BY 1, 2
        ),
        n AS (SELECT count(DISTINCT sample_index)::float total FROM per_sample)
        SELECT usage_zone,
               round(min(area)::numeric, 1) lo,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY area)::numeric, 1) med,
               round(max(area)::numeric, 1) hi,
               round((count(*) / (SELECT total FROM n))::numeric, 2) frequency
        FROM per_sample GROUP BY 1 ORDER BY med DESC
    """), {"u": uuid, "st": storey_type}).all()

    daylight = db.execute(text("""
        WITH per_sample AS (
            SELECT zs.sample_index,
                   sum(z.area_m2) FILTER (WHERE NOT z.has_daylight) / NULLIF(sum(z.area_m2), 0) share
            FROM zoning.zone z JOIN zoning.zone_set zs USING (zone_set_id)
            WHERE zs.bldg_uuid = :u AND z.storey_type = :st
            GROUP BY 1
        )
        SELECT round((min(share) * 100)::numeric, 1) lo,
               round((percentile_cont(0.5) WITHIN GROUP (ORDER BY share) * 100)::numeric, 1) med,
               round((max(share) * 100)::numeric, 1) hi
        FROM per_sample
    """), {"u": uuid, "st": storey_type}).first()

    return {
        "zones": [{"usage": r.usage_zone, "area_min": float(r.lo), "area_median": float(r.med),
                   "area_max": float(r.hi), "certainty": float(r.frequency)} for r in rows],
        "no_daylight_pct": ({"min": float(daylight.lo), "median": float(daylight.med),
                             "max": float(daylight.hi)} if daylight and daylight.med is not None else None),
    }
