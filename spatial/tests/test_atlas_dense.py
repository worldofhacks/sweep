"""Pipeline wiring fixtures, not evidence that synthetic inputs reconstruct a real scene."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from relay.atlas import AtlasStore
from relay.tests.test_atlas_reconstruction import seed
from spatial.atlas_assets import write_glb
from spatial.atlas_dense import ENGINES, dense_mesh, engine_files, ply_vertex_count
from spatial.tests.test_atlas_mesh_assets import fixture


def test_engine_directory_is_explicit_and_complete(tmp_path):
    with pytest.raises(ValueError, match="all four"):
        engine_files(tmp_path)


def test_pipeline_uses_dense_points_and_disables_synthetic_fill(tmp_path, monkeypatch):
    # Wiring tests must also run in core CI without installing the optional real engine.
    undistort = []
    monkeypatch.setitem(sys.modules, "pycolmap", SimpleNamespace(
        __version__="4.2.0",
        UndistortCameraOptions=lambda **kwargs: kwargs,
        undistort_images=lambda *a, **kw: undistort.append((a, kw)),
    ))
    store = AtlasStore(tmp_path / "store")
    try:
        job = store.queue_reconstruction(seed(store))
        store.claim_reconstruction()
        working = tmp_path / "working"
        working.mkdir()
        binaries = tmp_path / "binaries"
        binaries.mkdir()
        for name in ENGINES:
            path = binaries / name
            path.write_text("test-only, never executed")
            path.chmod(0o700)
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            assert kwargs["cwd"] == working
            if "--help" in command:
                return subprocess.CompletedProcess(command, 1, b"OpenMVS x64 v2.4.0", b"")
            assert command[-2:] == ["--max-threads", "4"]
            if command[0].endswith("DensifyPointCloud"):
                (working / "scene_dense.ply").write_bytes(
                    b"ply\nformat binary_little_endian 1.0\nelement vertex 540195\nend_header\n"
                )
            if command[0].endswith("TextureMesh"):
                document, binary = fixture(working)
                write_glb(working / "atlas_mesh.glb", document, binary)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(subprocess, "run", run)
        result = dense_mesh(store, job["id"], tmp_path / "images", tmp_path / "model",
                            working, working, binaries)
        assert result["representation"] == "textured_mesh" and result["faces"] == 50
        assert result["dense_points"] == 540195 and result["experimental"] is True
        assert len(result["dense_engines"]) == 4
        assert undistort[0][0][1:] == (tmp_path / "model", tmp_path / "images")
        assert undistort[0][1] == {"undistort_options": {"max_image_size": 1600}, "num_threads": 4}
        stages = [command for command in calls if "--help" not in command]
        densify, mesh, texture = stages[1:]
        for option, value in (("--tower-mode", "0"), ("--postprocess-dmaps", "1"),
                              ("--number-views-fuse", "3"), ("--max-resolution", "1280")):
            assert densify[densify.index(option) + 1] == value
        assert mesh[mesh.index("-p") + 1] == "scene_dense.ply"
        for command in (mesh, texture):
            assert command[command.index("--close-holes") + 1] == "0"
        for option in ("--global-seam-leveling", "--local-seam-leveling", "--sharpness-weight"):
            assert texture[texture.index(option) + 1] == "0"
        assert result["dense_settings"]["global_seam_leveling"] is False
        assert result["dense_settings"]["local_seam_leveling"] is False
        # Publishing ready is owned by the parent reconstruction only after provenance is saved.
        assert store.reconstruction_job(job["id"])["status"] == "texturing"
    finally:
        store.close()


@pytest.mark.parametrize("header", [b"not ply", b"ply\nelement vertex 9999999999\nend_header\n"])
def test_rejects_missing_or_unbounded_dense_counts(tmp_path, header):
    path = tmp_path / "cloud.ply"
    path.write_bytes(header)
    with pytest.raises(ValueError):
        ply_vertex_count(path)
