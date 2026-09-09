"""Optional local dense-engine integration; never downloads or deploys an engine.

OpenMVS 2.4.0 is an experimental operator-provided dependency. Its AGPL and included
research-only IBFS notice require a separate licensing review before production use.
"""

import hashlib
import os
import subprocess
from pathlib import Path

from relay.atlas import AtlasStore
from spatial.atlas_assets import pack_openmvs_glb

ENGINES = ("InterfaceCOLMAP", "DensifyPointCloud", "ReconstructMesh", "TextureMesh")


def engine_files(directory: Path) -> dict[str, Path]:
    directory = directory.resolve()
    result = {name: directory / name for name in ENGINES}
    if any(not path.is_file() or not os.access(path, os.X_OK) for path in result.values()):
        raise ValueError("The dense engine directory must contain all four OpenMVS executables.")
    return result


def dense_mesh(
    store: AtlasStore,
    job_id: str,
    images: Path,
    model_path: Path,
    working: Path,
    output: Path,
    engine_directory: Path,
) -> dict:
    import pycolmap

    binaries = engine_files(engine_directory)
    engines = []
    for name, path in binaries.items():
        # The pinned CLI returns 1 for --help. Read its bounded banner, not an exit-code guess.
        result = subprocess.run(
            [str(path), "--help"], cwd=working, capture_output=True, timeout=10, check=False
        )
        banner = result.stdout + result.stderr
        if b"v2.4.0" not in banner[:4096]:
            raise ValueError("This dense integration requires OpenMVS 2.4.0.")
        with path.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        engines.append({"name": name, "version": "2.4.0", "sha256": checksum})

    def stage(name, arguments, status, progress, detail):
        if not store.progress_reconstruction(
            job_id, status=status, progress=progress, detail=detail
        ):
            raise ValueError("This reconstruction is no longer active.")
        subprocess.run(
            [str(binaries[name]), *arguments, "--max-threads", "4"],
            cwd=working,
            check=True,
            timeout=900,
        )

    store.progress_reconstruction(
        job_id,
        status="densifying",
        progress=70,
        detail="Correcting lens distortion for surface reconstruction.",
    )
    undistorted = working / "dense"
    pycolmap.undistort_images(
        undistorted,
        model_path,
        images,
        undistort_options=pycolmap.UndistortCameraOptions(max_image_size=1600),
        num_threads=4,
    )
    stage(
        "InterfaceCOLMAP",
        ["-i", "dense", "-o", "scene.mvs", "--image-folder", str(undistorted / "images")],
        "densifying",
        72,
        "Connecting calibrated camera views to the surface solver.",
    )
    stage(
        "DensifyPointCloud",
        [
            "-i",
            "scene.mvs",
            "-o",
            "scene_dense.mvs",
            "--resolution-level",
            "0",
            "--max-resolution",
            "1280",
            "--number-views",
            "5",
            "--number-views-fuse",
            "3",
            "--geometric-iters",
            "2",
            "--postprocess-dmaps",
            "1",
            "--tower-mode",
            "0",
            "--estimate-roi",
            "0",
            "--crop-to-roi",
            "0",
        ],
        "densifying",
        76,
        "Estimating surface depth from agreement between overlapping views.",
    )
    dense_points = ply_vertex_count(working / "scene_dense.ply")
    # The .mvs interface retains sparse tracks: explicitly use the actual dense PLY.
    stage(
        "ReconstructMesh",
        [
            "-i",
            "scene_dense.mvs",
            "-p",
            "scene_dense.ply",
            "-o",
            "scene_mesh.mvs",
            "--target-face-num",
            "100000",
            "--remove-spurious",
            "4",
            "--close-holes",
            "0",
            "--smooth",
            "0",
            "--crop-to-roi",
            "0",
        ],
        "meshing",
        88,
        "Reconstructing a bounded surface without automatic hole filling.",
    )
    stage(
        "TextureMesh",
        [
            "-i",
            "scene_dense.mvs",
            "-m",
            "scene_mesh.ply",
            "-o",
            "atlas_mesh.mvs",
            "--export-type",
            "glb",
            "--max-texture-size",
            "2048",
            "--close-holes",
            "0",
            "--sharpness-weight",
            "0",
            "--empty-color",
            "8421504",
            # The pinned macOS build corrupts colors with seam leveling on the real-photo
            # acceptance set. Keep photographic patches without these color adjustments.
            "--global-seam-leveling",
            "0",
            "--local-seam-leveling",
            "0",
        ],
        "texturing",
        95,
        "Projecting source photographs onto the reconstructed surface.",
    )
    stats = pack_openmvs_glb(working / "atlas_mesh.glb", output / "cloud.glb")
    return {
        **stats,
        "dense_points": dense_points,
        "representation": "textured_mesh",
        "engine": f"COLMAP {pycolmap.__version__} / OpenMVS 2.4.0",
        "dense_engines": engines,
        "experimental": True,
        "hole_filling": False,
        "dense_settings": {
            "max_resolution": 1280,
            "agreement_views": 3,
            "target_faces": 100000,
            "max_texture_size": 2048,
            "global_seam_leveling": False,
            "local_seam_leveling": False,
            "tower_mode": 0,
            "depth_postprocess": "remove_speckles_only",
        },
    }


def ply_vertex_count(path: Path) -> int:
    """Read only a bounded PLY header, never load the dense intermediate into Python."""
    with path.open("rb") as handle:
        header = handle.read(8192).split(b"end_header\n", 1)
    if len(header) != 2 or not header[0].startswith(b"ply\n"):
        raise ValueError("The dense engine did not produce a complete point-cloud header.")
    entries = [line for line in header[0].splitlines() if line.startswith(b"element vertex ")]
    if len(entries) != 1:
        raise ValueError("The dense point count is missing.")
    count = int(entries[0].split()[-1])
    if not 50 <= count <= 10_000_000:
        raise ValueError("The dense point count is outside this worker's limits.")
    return count
