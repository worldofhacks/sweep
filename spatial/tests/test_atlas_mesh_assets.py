"""Encoding fixtures validate transport, not the quality of image reconstruction."""

import base64
import hashlib

import cv2
import numpy as np
import pytest

from spatial.atlas_assets import pack_openmvs_glb, read_glb, write_glb


def fixture(tmp_path):
    xyz = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype="<f4").tobytes()
    indices = np.tile(np.array([0, 1, 2], dtype="<u4"), 50).tobytes()
    uv = np.array([[0, 0], [1, 0], [0, 1]], dtype="<f4").tobytes()
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "TEXCOORD_0": 2},
                        "indices": 1,
                        "mode": 4,
                        "material": 0,
                    }
                ]
            }
        ],
        "buffers": [
            {"byteLength": len(xyz) + len(indices)},
            {
                "byteLength": len(uv),
                "uri": "data:application/octet-stream;base64," + base64.b64encode(uv).decode(),
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteLength": len(xyz)},
            {"buffer": 0, "byteOffset": len(xyz), "byteLength": len(indices)},
            {"buffer": 1, "byteLength": len(uv)},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": 3},
            {"bufferView": 1, "componentType": 5125, "type": "SCALAR", "count": 150},
            {"bufferView": 2, "componentType": 5126, "type": "VEC2", "count": 3},
        ],
        "images": [{"uri": "texture.png"}],
        "textures": [{"source": 0}],
        "materials": [
            {
                "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}},
                "extensions": {"KHR_materials_unlit": {}},
            }
        ],
        "extensionsUsed": ["KHR_materials_unlit"],
    }
    ok, image = cv2.imencode(".png", np.full((4, 4, 3), 127, dtype=np.uint8))
    assert ok
    (tmp_path / "texture.png").write_bytes(image.tobytes())
    return document, xyz + indices


def test_pack_embeds_unchanged_geometry_and_textures(tmp_path):
    document, binary = fixture(tmp_path)
    write_glb(tmp_path / "engine.glb", document, binary)
    stats = pack_openmvs_glb(tmp_path / "engine.glb", tmp_path / "cloud.glb")
    packed, data = read_glb(tmp_path / "cloud.glb")
    assert stats["faces"] == 50 and stats["vertices"] == 3
    checksum = hashlib.sha256((tmp_path / "cloud.glb").read_bytes()).hexdigest()
    assert stats["artifact_sha256"] == checksum
    assert len(packed["buffers"]) == 1 and "uri" not in packed["buffers"][0]
    assert data[: len(binary)] == binary
    image = packed["images"][0]
    assert "uri" not in image and image["mimeType"] == "image/png"
    view = packed["bufferViews"][image["bufferView"]]
    assert (
        data[view["byteOffset"] : view["byteOffset"] + view["byteLength"]]
        == (tmp_path / "texture.png").read_bytes()
    )
    assert packed["extras"]["metric_scale"] is False
    assert packed["extras"]["hole_filling"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "remote",
        "path",
        "symlink",
        "index",
        "count",
        "nan",
        "texture",
        "material",
        "extension",
        "texture_source",
    ],
)
def test_pack_rejects_incomplete_or_unbounded_assets(tmp_path, fault):
    document, binary = fixture(tmp_path)
    if fault == "remote":
        document["buffers"][1]["uri"] = "https://example.test/geometry.bin"
    elif fault == "path":
        document["images"][0]["uri"] = "../texture.png"
    elif fault == "symlink":
        (tmp_path / "link.png").symlink_to(tmp_path / "texture.png")
        document["images"][0]["uri"] = "link.png"
    elif fault == "index":
        binary = binary[:36] + np.full(150, 4, dtype="<u4").tobytes()
    elif fault == "count":
        document["accessors"][0]["count"] = 1_000_000_000
    elif fault == "nan":
        binary = np.array([np.nan], dtype="<f4").tobytes() + binary[4:]
    elif fault == "texture":
        (tmp_path / "texture.png").write_bytes((tmp_path / "texture.png").read_bytes()[:33])
    elif fault == "material":
        document["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]["index"] = 2
    elif fault == "extension":
        document["textures"][0]["extensions"] = {"EXT_texture_webp": {"source": 0}}
    elif fault == "texture_source":
        document["textures"][0]["source"] = 3
    write_glb(tmp_path / "engine.glb", document, binary)
    with pytest.raises(ValueError):
        pack_openmvs_glb(tmp_path / "engine.glb", tmp_path / "cloud.glb")
    assert not (tmp_path / "cloud.glb").exists()
