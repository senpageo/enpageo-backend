"""Geometrie und GLB-Writer für die 3D-Tiles-Pipeline.

Eine Kachel = ein GLB mit einem Mesh in einem lokalen Ost-Nord-Oben-Rahmen um die Kachelmitte; die
Matrix ENU -> ECEF steht im tileset.json als tile.transform. Jedes Dreieck trägt die Feature-ID seines
2D-Gebäudes (EXT_mesh_features), die Eigenschaftstabelle (EXT_structural_metadata) enthält uuid und Stufe.

Kompakt gespeichert (KHR_mesh_quantization): Positionen als uint16 mit einheitlichem Maßstab im Knoten
(einheitlich, damit Normalen nicht verzerrt werden), Normalen als int8, Feature-IDs als uint16.
Ergibt 16 statt 28 Byte je Vertex.
"""
import json
import math
import struct
from pathlib import Path

import mapbox_earcut
import numpy as np
from pyproj import Transformer

ROOF, WALL = 33, 34
to_wgs84 = Transformer.from_crs("EPSG:25833", "EPSG:4326", always_xy=True)

WGS84_A = 6378137.0
WGS84_E2 = 6.69437999014e-3


def geodetic_to_ecef(lon_deg, lat_deg, h):
    lon, lat = np.radians(lon_deg), np.radians(lat_deg)
    n = WGS84_A / np.sqrt(1 - WGS84_E2 * np.sin(lat) ** 2)
    x = (n + h) * np.cos(lat) * np.cos(lon)
    y = (n + h) * np.cos(lat) * np.sin(lon)
    z = (n * (1 - WGS84_E2) + h) * np.sin(lat)
    return np.stack([x, y, z], axis=-1)


def enu_frame(lon_deg, lat_deg):
    lon, lat = math.radians(lon_deg), math.radians(lat_deg)
    e = np.array([-math.sin(lon), math.cos(lon), 0.0])
    n = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    u = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    return e, n, u


def enu_transform(x, y):
    """4x4-Matrix (Spalten zuerst, wie 3D Tiles sie erwartet) ENU -> ECEF am Punkt x/y in EPSG:25833."""
    lon, lat = to_wgs84.transform(x, y)
    e, n, u = enu_frame(lon, lat)
    o = geodetic_to_ecef(lon, lat, 0.0)
    return [*e, 0.0, *n, 0.0, *u, 0.0, *o.tolist(), 1.0]


def mat(cols):
    return np.array(cols, dtype=np.float64).reshape(4, 4).T


def cols(m):
    return m.T.reshape(-1).tolist()


# ---------------------------------------------------------------------------------------------- Geometrie

def triangulate(rings):
    """Planares Polygon mit Löchern (Ringe, geschlossen, xy oder xyz) -> (verts Nx3, tris Mx3) oder None."""
    pts, ends = [], []
    for r in rings:
        r = r[:-1] if len(r) > 1 and r[0] == r[-1] else r
        if len(r) < 3:
            continue
        pts.extend(r)
        ends.append(len(pts))
    if not ends:
        return None
    v = np.asarray(pts, dtype=np.float64)
    if v.shape[1] == 2:
        v = np.column_stack([v, np.zeros(len(v))])
    # Newell-Normale des Außenrings -> Projektionsebene wählen
    o = v[: ends[0]]
    a, b = o, np.roll(o, -1, axis=0)
    nrm = np.array([((a[:, 1] - b[:, 1]) * (a[:, 2] + b[:, 2])).sum(),
                    ((a[:, 2] - b[:, 2]) * (a[:, 0] + b[:, 0])).sum(),
                    ((a[:, 0] - b[:, 0]) * (a[:, 1] + b[:, 1])).sum()])
    if np.linalg.norm(nrm) < 1e-6:
        return None
    keep = [i for i in range(3) if i != int(np.argmax(np.abs(nrm)))]
    tris = mapbox_earcut.triangulate_float64(v[:, keep] - v[0, keep], np.asarray(ends, dtype=np.uint32))
    if len(tris) == 0:
        return None
    return v, tris.reshape(-1, 3)


def extrude(footprint_geojson, height):
    """Box aus 2D-Grundriss (mit Höfen): Dach + Wände, ohne Boden. z relativ zum Boden."""
    g = footprint_geojson
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    out = []
    for rings in polys:
        out.append((ROOF, [[[p[0], p[1], height] for p in r] for r in rings]))
        for r in rings:
            for p0, p1 in zip(r[:-1], r[1:]):
                out.append((WALL, [[[p0[0], p0[1], 0], [p1[0], p1[1], 0], [p1[0], p1[1], height],
                                    [p0[0], p0[1], height], [p0[0], p0[1], 0]]]))
    return out


class MeshBuilder:
    """Sammelt Dreiecke aller Gebäude einer Kachel (flache Schattierung: jede Fläche eigene Vertices)."""

    def __init__(self):
        self.pos, self.fid, self.idx = [], [], []
        self.n = 0
        self.uuids, self.tiers = [], []

    def add_building(self, uuid, tier, surfaces, z_base):
        fid = len(self.uuids)
        added = False
        for _cls, rings in surfaces:
            res = triangulate(rings)
            if res is None:
                continue
            v, tris = res
            v[:, 2] -= z_base
            self.pos.append(v)
            self.fid.append(np.full(len(v), fid, dtype=np.uint32))
            self.idx.append(tris.astype(np.int64) + self.n)
            self.n += len(v)
            added = True
        if added:
            self.uuids.append(uuid)
            self.tiers.append(tier)
        return added


# ---------------------------------------------------------------------------------------------- GLB

def _pad(b, align=8, fill=b"\x00"):
    return b + fill * ((-len(b)) % align)


def _strings(vals):
    enc = [s.encode("utf-8") for s in vals]
    offs = np.zeros(len(enc) + 1, dtype=np.uint32)
    offs[1:] = np.cumsum([len(x) for x in enc])
    return b"".join(enc), offs.tobytes()


def write_glb(mb: MeshBuilder, path: Path, center_xy):
    """Schreibt die Kachel; gibt (transform, zmin, zmax, vertices, dreiecke) zurück."""
    pos = np.concatenate(mb.pos)
    idx = np.concatenate(mb.idx).reshape(-1, 3)
    fid = np.concatenate(mb.fid)

    lon0, lat0 = to_wgs84.transform(*center_xy)
    origin = geodetic_to_ecef(lon0, lat0, 0.0)
    e, n, u = enu_frame(lon0, lat0)
    lon, lat = to_wgs84.transform(pos[:, 0], pos[:, 1])
    ecef = geodetic_to_ecef(lon, lat, pos[:, 2]) - origin
    gl = np.stack([ecef @ e, ecef @ u, -(ecef @ n)], axis=-1)  # glTF ist y-oben: (Ost, Oben, -Nord)

    # flache Normalen je Dreieck, auf Vertices verteilt (Vertices sind je Fläche dupliziert)
    a, b, c = gl[idx[:, 0]], gl[idx[:, 1]], gl[idx[:, 2]]
    fn = np.cross(b - a, c - a)
    nrm = np.zeros_like(gl)
    for k in range(3):
        np.add.at(nrm, idx[:, k], fn)
    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.where(ln > 0, nrm / np.maximum(ln, 1e-12), [0.0, 1.0, 0.0])

    # Quantisierung: einheitlicher Maßstab über alle Achsen
    lo = gl.min(axis=0)
    scale = max(float((gl.max(axis=0) - lo).max()) / 65535.0, 1e-6)
    q = np.zeros((len(gl), 4), dtype=np.uint16)  # 4. Komponente = Füllung auf 8 Byte
    q[:, :3] = np.clip(np.round((gl - lo) / scale), 0, 65535)
    qn = np.zeros((len(gl), 4), dtype=np.int8)
    qn[:, :3] = np.clip(np.round(nrm * 127), -127, 127)
    if len(mb.uuids) < 65536:
        qf = np.zeros((len(gl), 2), dtype=np.uint16)
        qf[:, 0] = fid
        fid_blob, fid_type, fid_stride = qf.tobytes(), 5123, 4
    else:
        fid_blob, fid_type, fid_stride = fid.astype(np.float32).tobytes(), 5126, None
    idx_dtype, idx_type = (np.uint16, 5123) if len(gl) < 65536 else (np.uint32, 5125)

    uuid_v, uuid_o = _strings(mb.uuids)
    tier_v, tier_o = _strings(mb.tiers)
    blobs = [(q.tobytes(), 8, 34962), (qn.tobytes(), 4, 34962), (fid_blob, fid_stride, 34962),
             (idx.astype(idx_dtype).tobytes(), None, 34963),
             (uuid_v, None, None), (uuid_o, None, None), (tier_v, None, None), (tier_o, None, None)]
    views, binbuf = [], b""
    for blob, stride, target in blobs:
        binbuf = _pad(binbuf)
        view = {"buffer": 0, "byteOffset": len(binbuf), "byteLength": len(blob)}
        if stride:
            view["byteStride"] = stride
        if target:
            view["target"] = target
        views.append(view)
        binbuf += blob
    binbuf = _pad(binbuf)

    qmin, qmax = q[:, :3].min(axis=0), q[:, :3].max(axis=0)
    gltf = {
        "asset": {"version": "2.0", "generator": "enpageo tiles/glb.py"},
        "extensionsUsed": ["KHR_mesh_quantization", "EXT_mesh_features", "EXT_structural_metadata"],
        "extensionsRequired": ["KHR_mesh_quantization"],
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": lo.tolist(), "scale": [scale, scale, scale]}],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1, "_FEATURE_ID_0": 2},
            "indices": 3, "material": 0, "mode": 4,
            "extensions": {"EXT_mesh_features": {"featureIds": [
                {"featureCount": len(mb.uuids), "attribute": 0, "propertyTable": 0}]}},
        }]}],
        "materials": [{
            "pbrMetallicRoughness": {"baseColorFactor": [0.92, 0.92, 0.90, 1.0], "metallicFactor": 0.0,
                                     "roughnessFactor": 1.0},
            "doubleSided": True,
        }],
        "accessors": [
            {"bufferView": 0, "componentType": 5123, "count": len(gl), "type": "VEC3",
             "min": qmin.tolist(), "max": qmax.tolist()},
            {"bufferView": 1, "componentType": 5120, "normalized": True, "count": len(gl), "type": "VEC3"},
            {"bufferView": 2, "componentType": fid_type, "count": len(gl), "type": "SCALAR"},
            {"bufferView": 3, "componentType": idx_type, "count": int(idx.size), "type": "SCALAR"},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(binbuf)}],
        "extensions": {"EXT_structural_metadata": {
            "schema": {"id": "enpageo", "classes": {"building": {"properties": {
                "uuid": {"type": "STRING"}, "tier": {"type": "STRING"}}}}},
            "propertyTables": [{"class": "building", "count": len(mb.uuids), "properties": {
                "uuid": {"values": 4, "stringOffsets": 5, "stringOffsetType": "UINT32"},
                "tier": {"values": 6, "stringOffsets": 7, "stringOffsetType": "UINT32"},
            }}],
        }},
    }
    js = _pad(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), 4, b" ")
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(binbuf)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(binbuf), 0x004E4942) + binbuf)

    transform = [*e, 0.0, *n, 0.0, *u, 0.0, *origin.tolist(), 1.0]
    return transform, float(pos[:, 2].min()), float(pos[:, 2].max()), len(gl), len(idx)


def region_of(x0, y0, size, hmin, hmax):
    """Quadrat in EPSG:25833 -> 3D-Tiles-region (Bogenmaß, Höhen in m), leicht gepuffert."""
    xs = np.array([x0, x0 + size, x0, x0 + size])
    ys = np.array([y0, y0, y0 + size, y0 + size])
    lon, lat = to_wgs84.transform(xs, ys)
    pad = 1e-6
    return [math.radians(lon.min()) - pad, math.radians(lat.min()) - pad,
            math.radians(lon.max()) + pad, math.radians(lat.max()) + pad, hmin - 1, hmax + 1]
