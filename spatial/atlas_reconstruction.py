"""Image-derived sparse geometry, isolated from the relay and all motion authority.

Only the optional Atlas worker imports COLMAP. No generated scene, GPS camera layout,
or unregistered image is passed off as reconstructed geometry.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path

# Bound untrusted source-image allocation before importing OpenCV in this child process.
os.environ.setdefault("OPENCV_IO_MAX_IMAGE_PIXELS", "40000000")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from relay.atlas import AtlasStore  # noqa: E402
from spatial.atlas_assets import write_glb  # noqa: E402

MAX_IMAGES = 120
MAX_POINTS = 120_000


def write_cloud_glb(path: Path, xyz: np.ndarray, colors: np.ndarray) -> str:
    """A self-contained glTF POINTS asset; no URLs, generated surfaces, or invented texture."""
    xyz = np.asarray(xyz, dtype="<f4")
    colors = np.asarray(colors, dtype="u1")
    if xyz.ndim != 2 or xyz.shape[1] != 3 or not 1 <= len(xyz) <= MAX_POINTS:
        raise ValueError("The reconstruction did not produce a bounded point cloud.")
    if colors.shape != xyz.shape or not np.isfinite(xyz).all():
        raise ValueError("The reconstructed points are invalid.")
    positions = xyz.tobytes()
    rgba = np.column_stack((colors, np.full(len(colors), 255, dtype="u1"))).tobytes()
    binary = positions + rgba
    document = {
        "asset": {"version": "2.0", "generator": "Sweep Atlas / COLMAP 4.2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "COLOR_0": 1}, "mode": 0}]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(positions), "target": 34962},
            {"buffer": 0, "byteOffset": len(positions), "byteLength": len(rgba), "target": 34962},
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(xyz),
                "type": "VEC3",
                "min": xyz.min(axis=0).tolist(),
                "max": xyz.max(axis=0).tolist(),
            },
            {
                "bufferView": 1,
                "componentType": 5121,
                "normalized": True,
                "count": len(xyz),
                "type": "VEC4",
            },
        ],
        "extras": {
            "representation": "sparse_point_cloud",
            "metric_scale": False,
            "coordinate_frame": "COLMAP local relative; not georeferenced",
        },
    }
    return write_glb(path, document, binary)


def prepare_images(store: AtlasStore, job: dict, directory: Path) -> list[dict]:
    images = []
    videos = sum(source["kind"] == "video" for source in job["sources"])
    photos = len(job["sources"]) - videos
    video_budget = min(24, max(1, (MAX_IMAGES - photos) // max(1, videos)))
    for source in reversed(job["sources"]):
        path = store.media / source["id"]
        with path.open("rb") as handle:
            checksum = hashlib.file_digest(handle, "sha256").hexdigest()
        if checksum != source["sha256"]:
            raise ValueError(
                "An original capture failed its checksum check. No model was published."
            )

        def save(frame, timestamp_ms=None, source=source, checksum=checksum):
            if len(images) >= MAX_IMAGES:
                return
            if frame is None or min(frame.shape[:2]) < 120 or frame.size > 40_000_000 * 3:
                raise ValueError(
                    "A source is unreadable, too small, or exceeds the image-size limit."
                )
            height, width = frame.shape[:2]
            scale = min(1, 1600 / max(height, width))
            if scale < 1:
                frame = cv2.resize(
                    frame,
                    (round(width * scale), round(height * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            name = f"view-{len(images):04d}.jpg"
            if not cv2.imwrite(str(directory / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 95]):
                raise ValueError("The worker could not write a prepared camera view.")
            images.append(
                {
                    "name": name,
                    "capture_id": source["id"],
                    "source_sha256": checksum,
                    "video_offset_ms": timestamp_ms,
                }
            )

        if source["kind"] != "video":
            save(cv2.imread(str(path)))
        else:
            video = cv2.VideoCapture(str(path))
            try:
                if not video.isOpened():
                    raise ValueError("A video could not be decoded. Try overlapping still photos.")
                # Sample actual decoded frames at one-second intervals, up to 24 per video.
                for index in range(video_budget):
                    if len(images) >= MAX_IMAGES:
                        break
                    video.set(cv2.CAP_PROP_POS_MSEC, index * 1000)
                    ok, frame = video.read()
                    if not ok:
                        break
                    save(frame, round(video.get(cv2.CAP_PROP_POS_MSEC)))
            finally:
                video.release()
    if len(images) < 3:
        raise ValueError(
            "At least three usable overlapping views are needed. Capture from different positions."
        )
    return images


def reconstruct(store: AtlasStore, job: dict, openmvs_bin: Path | None = None) -> None:
    import pycolmap

    store.progress_reconstruction(
        job["id"], representation="textured_mesh" if openmvs_bin else "sparse_point_cloud",
        experimental=openmvs_bin is not None,
    )
    output = store.root / "reconstructions" / job["id"]
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="processing-", dir=output) as temporary:
        root = Path(temporary)
        images = root / "images"
        images.mkdir()
        manifest = prepare_images(store, job, images)
        database = root / "features.db"
        sparse = root / "sparse"
        sparse.mkdir()
        store.progress_reconstruction(
            job["id"],
            status="extracting",
            progress=15,
            detail=f"Finding distinctive features in {len(manifest)} camera views.",
        )
        extraction = pycolmap.FeatureExtractionOptions()
        extraction.max_image_size = 1600
        extraction.num_threads = 4
        extraction.sift.max_num_features = 8192
        pycolmap.extract_features(
            database,
            images,
            camera_mode=pycolmap.CameraMode.PER_IMAGE,
            extraction_options=extraction,
            device=pycolmap.Device.cpu,
        )
        store.progress_reconstruction(
            job["id"],
            status="matching",
            progress=35,
            detail="Matching overlapping details between camera views.",
        )
        matching = pycolmap.FeatureMatchingOptions()
        matching.num_threads = 4
        pycolmap.match_exhaustive(database, matching_options=matching, device=pycolmap.Device.cpu)
        store.progress_reconstruction(
            job["id"],
            status="mapping",
            progress=60,
            detail="Solving camera poses and triangulating observed 3D points.",
        )
        options = pycolmap.IncrementalPipelineOptions()
        options.num_threads = 4
        options.min_model_size = 3
        options.max_num_models = 5
        options.max_runtime_seconds = 600
        models = pycolmap.incremental_mapping(database, images, sparse, options=options)
        if not models:
            raise ValueError(
                "The views could not be connected in 3D. Add well-lit, overlapping views "
                "from different positions; avoid only rotating in place."
            )
        model = max(models.values(), key=lambda value: value.num_reg_images())
        if model.num_reg_images() < 3 or model.num_points3D() < 50:
            raise ValueError(
                "Too few stable points could be reconstructed. Add more overlapping captures."
            )
        points = [
            p
            for p in model.points3D.values()
            if p.track.length() >= 3 and p.error <= 4 and np.isfinite(p.xyz).all()
        ]
        if len(points) < 50:
            raise ValueError(
                "Too few 3D points passed the reprojection checks. Add clearer overlapping views."
            )
        points.sort(key=lambda point: (-point.track.length(), point.error))
        shown = points[:MAX_POINTS]
        checksum = write_cloud_glb(
            output / "cloud.glb",
            np.array([p.xyz for p in shown]),
            np.array([p.color for p in shown]),
        )
        by_name = {item["name"]: item for item in manifest}
        cameras = [
            {
                **by_name[image.name],
                "center": image.projection_center().tolist(),
                "camera_id": image.camera_id,
            }
            for image in model.images.values()
            if image.has_pose
        ]
        source_count = len({item["capture_id"] for item in cameras})
        provenance = {
            "engine": f"COLMAP {pycolmap.__version__}",
            "job_id": job["id"],
            "representation": "sparse_point_cloud",
            "metric_scale": False,
            "coordinate_frame": "local_relative",
            "source_views": manifest,
            "cameras": cameras,
            "prepared_views": len(manifest),
            "registered_views": model.num_reg_images(),
            "registered_captures": source_count,
            "components": len(models),
            "filtered_points": len(points),
            "displayed_points": len(shown),
            "mean_reprojection_error_px": float(np.mean([p.error for p in shown])),
            "artifact_sha256": checksum,
        }
        # Retain the calibrated camera/track solution for densification and provenance.
        model.write(output)
        detail = ("Sparse 3D geometry reconstructed from matching image features. "
                  "Dense surfaces are not reconstructed yet.")
        if openmvs_bin is not None:
            from spatial.atlas_dense import dense_mesh

            dense = dense_mesh(store, job["id"], images, output, root, output, openmvs_bin)
            provenance.update(dense)
            checksum = dense["artifact_sha256"]
            detail = ("Photo-textured surfaces reconstructed from overlapping images. "
                      "Experimental local build; gaps remain and scale is not geographic.")
        (output / "manifest.json").write_text(json.dumps(provenance, allow_nan=False))
        store.progress_reconstruction(
            job["id"],
            status="ready",
            progress=100,
            detail=detail,
            representation=provenance["representation"],
            engine=provenance["engine"],
            experimental=provenance.get("experimental", False),
            faces=provenance.get("faces"),
            vertices=provenance.get("vertices"),
            dense_points=provenance.get("dense_points"),
            artifact_sha256=checksum,
            artifact_bytes=(output / "cloud.glb").stat().st_size,
            registered_views=model.num_reg_images(),
            prepared_views=len(manifest),
            registered_captures=source_count,
            points=len(shown),
            mean_reprojection_error_px=provenance["mean_reprojection_error_px"],
            components=len(models),
        )
