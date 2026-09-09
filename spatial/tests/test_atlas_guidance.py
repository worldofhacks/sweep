"""Topology fixtures are algorithm checks, not reconstruction-quality acceptance."""

import numpy as np
import pytest

from spatial.atlas_assets import pack_openmvs_glb, write_glb
from spatial.atlas_guidance import mesh_guidance, review_regions
from spatial.tests.test_atlas_mesh_assets import fixture


def plane():
    vertices = np.array([[x, y, 0] for y in range(25) for x in range(25)])
    faces = []
    for y in range(24):
        for x in range(24):
            a = y * 25 + x
            faces.extend([[a, a + 1, a + 25], [a + 1, a + 26, a + 25]])
    return vertices, np.array(faces)


def test_actual_open_edges_are_bounded_and_not_completeness_or_geography():
    vertices, faces = plane()
    result = review_regions(vertices, faces)
    assert result["boundary_edges"] == 96
    assert result["nonmanifold_edges"] == 0
    assert 1 <= len(result["regions"]) <= 6
    assert result["coordinate_frame"] == "local_relative" and result["metric_scale"] is False
    assert "confirmed missing surfaces" in result["meaning"]
    assert "latitude" not in result and "percent" not in result
    for region in result["regions"]:
        assert 2 <= len(region["segments"]) <= 256
        points = np.array(region["segments"])
        assert np.all(np.linalg.norm(points - region["center"], axis=1) <= region["radius"] + 1e-8)
        # Every endpoint is an original boundary vertex, not a generated surface.
        assert all((point[0] in (0, 24) or point[1] in (0, 24)) for point in points)


def test_texture_seams_and_duplicate_faces_do_not_create_or_hide_edges():
    vertices, faces = plane()
    original = review_regions(vertices, faces)
    split = vertices[faces].reshape(-1, 3)
    seams = np.arange(len(split)).reshape(-1, 3)
    assert review_regions(split, seams) == original
    assert review_regions(vertices, np.concatenate([faces, faces[:, ::-1]])) == original


def test_closed_surface_does_not_claim_complete_coverage():
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
    faces = np.array([[0, 1, 2], [0, 1, 3], [0, 2, 3], [1, 2, 3]])
    result = review_regions(vertices, faces)
    assert result["boundary_edges"] == 0 and result["regions"] == []
    assert "percent" not in result


def test_nonmanifold_edge_is_reported_not_mislabeled_as_open():
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [0, -1, 0]])
    result = review_regions(vertices, np.array([[0, 1, 2], [0, 1, 3], [0, 1, 4]]))
    assert result["nonmanifold_edges"] == 1 and result["boundary_edges"] == 6


@pytest.mark.parametrize("fault", ["nan", "index", "float", "empty", "shape"])
def test_invalid_geometry_is_not_published(fault):
    vertices, faces = plane()
    vertices = vertices.astype(float)
    if fault == "nan":
        vertices[0, 0] = np.nan
    elif fault == "index":
        faces[0, 0] = len(vertices)
    elif fault == "float":
        faces = faces.astype(float)
    elif fault == "empty":
        faces = faces[:0]
    else:
        vertices = vertices[:, :2]
    with pytest.raises(ValueError):
        review_regions(vertices, faces)


def test_guidance_reads_the_validated_packed_mesh(tmp_path):
    document, binary = fixture(tmp_path)
    write_glb(tmp_path / "engine.glb", document, binary)
    pack_openmvs_glb(tmp_path / "engine.glb", tmp_path / "cloud.glb")
    assert mesh_guidance(tmp_path / "cloud.glb")["boundary_edges"] == 3
