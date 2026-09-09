"""Bounded review candidates from actual mesh topology, not surface completeness.

An edge incident to one triangle is an open model boundary. It may be a real
object edge, reconstruction failure, or the end of the scan. No missing surface,
safe viewpoint, metric distance, or geographic position is inferred from it.
"""

import hashlib
from pathlib import Path

import numpy as np

from spatial.atlas_assets import MAX_FACES, MAX_VERTICES, mesh_stats, read_glb

MAX_REGIONS = 6
MAX_SEGMENTS = 128
METHOD = "open-mesh-edges-v1"


def review_regions(vertices: np.ndarray, faces: np.ndarray) -> dict:
    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces)
    if (
        vertices.ndim != 2
        or vertices.shape[1] != 3
        or not 3 <= len(vertices) <= MAX_VERTICES
        or not np.isfinite(vertices).all()
        or faces.ndim != 2
        or faces.shape[1] != 3
        or not 1 <= len(faces) <= MAX_FACES
        or faces.dtype.kind not in "iu"
        or faces.min() < 0
        or faces.max() >= len(vertices)
    ):
        raise ValueError("Surface review requires bounded finite triangle geometry.")
    # Texture seams duplicate vertices. Weld exact positions across all primitives;
    # do not round nearby but distinct geometry into a fabricated closed surface.
    xyz, welded = np.unique(vertices, axis=0, return_inverse=True)
    triangles = np.sort(welded[faces], axis=1)
    valid = (triangles[:, 0] != triangles[:, 1]) & (triangles[:, 1] != triangles[:, 2])
    triangles = np.unique(triangles[valid], axis=0)
    edges, counts = np.unique(
        np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [0, 2]]]),
        axis=0,
        return_counts=True,
    )
    boundary = edges[counts == 1]
    result = {
        "method": METHOD,
        "coordinate_frame": "local_relative",
        "metric_scale": False,
        "meaning": "Open model edges to inspect, not confirmed missing surfaces or safe routes.",
        "boundary_edges": len(boundary),
        "nonmanifold_edges": int(np.sum(counts > 2)),
        "regions": [],
    }
    if not len(boundary):
        return result
    # Localize long outer boundaries as well as smaller disconnected loops. A fixed
    # 4x4x4 partition bounds the work; ranking is by edge length, NOT urgency/accuracy.
    low, high = xyz.min(axis=0), xyz.max(axis=0)
    extent = high - low
    scale = float(np.linalg.norm(extent))
    if not np.isfinite(scale) or scale <= 0:
        return result
    segments = xyz[boundary]
    lengths = np.linalg.norm(segments[:, 1] - segments[:, 0], axis=1)
    midpoints = segments.mean(axis=1)
    bins = np.minimum(3, ((midpoints - low) / np.maximum(extent, scale * 1e-9) * 4).astype(int))
    groups = []
    for cell in np.unique(bins, axis=0):
        indexes = np.flatnonzero(np.all(bins == cell, axis=1))
        length = float(lengths[indexes].sum())
        if len(indexes) < 3 or length < scale * 0.01:
            continue
        groups.append((length, tuple(cell), indexes))
    groups.sort(key=lambda value: (-value[0], value[1]))
    result["candidate_regions"] = len(groups)
    for _, _, indexes in groups[:MAX_REGIONS]:
        points = segments[indexes].reshape(-1, 3)
        center = (points.min(axis=0) + points.max(axis=0)) / 2
        radius = float(np.linalg.norm(points - center, axis=1).max())
        sampled = indexes[
            np.linspace(0, len(indexes) - 1, min(len(indexes), MAX_SEGMENTS), dtype=int)
        ]
        identity = hashlib.sha256(segments[indexes].astype("<f8").tobytes()).hexdigest()[:16]
        result["regions"].append(
            {
                "id": identity,
                "label": f"Region {len(result['regions']) + 1}",
                "center": center.tolist(),
                "radius": radius,
                "boundary_edges": len(indexes),
                "segments": segments[sampled].reshape(-1, 3).tolist(),
            }
        )
    return result


def mesh_guidance(path: Path) -> dict:
    document, binary = read_glb(path)
    return guidance_from_glb(document, binary)


def guidance_from_glb(document: dict, binary: bytes) -> dict:
    mesh_stats(document, binary)

    def read(index):
        accessor = document["accessors"][index]
        view = document["bufferViews"][accessor["bufferView"]]
        width = 3 if accessor["type"] == "VEC3" else 1
        dtype = {5126: "<f4", 5125: "<u4", 5123: "<u2"}[accessor["componentType"]]
        return np.frombuffer(
            binary,
            dtype,
            accessor["count"] * width,
            view.get("byteOffset", 0) + accessor.get("byteOffset", 0),
        ).reshape(-1, width)

    vertices, faces, offset = [], [], 0
    for primitive in document["meshes"][0]["primitives"]:
        points = read(primitive["attributes"]["POSITION"])
        faces.append(read(primitive["indices"]).astype(np.int64).reshape(-1, 3) + offset)
        vertices.append(points)
        offset += len(points)
    return review_regions(np.concatenate(vertices), np.concatenate(faces))
