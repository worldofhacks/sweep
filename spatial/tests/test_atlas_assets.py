"""Self-contained point asset encoding; these fixtures do not prove image reconstruction."""

import hashlib
import json
import struct

import numpy as np
import pytest

from spatial.atlas_reconstruction import write_cloud_glb


def test_glb_preserves_measured_points_and_color(tmp_path):
    xyz = np.array([[1.0, 2.0, 3.0], [2.0, -1.0, 0.5]], dtype=np.float32)
    rgb = np.array([[0, 10, 255], [180, 100, 50]], dtype=np.uint8)
    output = tmp_path / "cloud.glb"
    checksum = write_cloud_glb(output, xyz, rgb)
    data = output.read_bytes()
    assert checksum == hashlib.sha256(data).hexdigest()
    assert struct.unpack("<4sII", data[:12]) == (b"glTF", 2, len(data))
    size, kind = struct.unpack("<I4s", data[12:20])
    assert kind == b"JSON"
    document = json.loads(data[20 : 20 + size])
    assert document["meshes"][0]["primitives"][0]["mode"] == 0
    assert document["extras"]["metric_scale"] is False
    binary = data[28 + size :]
    np.testing.assert_array_equal(np.frombuffer(binary[:24], dtype="<f4").reshape(-1, 3), xyz)
    np.testing.assert_array_equal(np.frombuffer(binary[24:], dtype="u1").reshape(-1, 4)[:, :3], rgb)
    assert not list(tmp_path.glob("*.pending"))


def test_glb_rejects_nonfinite_geometry(tmp_path):
    with pytest.raises(ValueError, match="invalid"):
        write_cloud_glb(tmp_path / "cloud.glb", np.array([[np.nan, 0, 0]]), np.zeros((1, 3)))
    assert not (tmp_path / "cloud.glb").exists()
