"""Diagnostic pinhole-intrinsics candidates from recorded AprilTag corners."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import acos, atan, degrees, isfinite
from pathlib import Path

import cv2
import numpy as np

from calibration.intrinsics import (
    _FISHEYE_CALIBRATION_FLAGS,
    _FISHEYE_CRITERIA,
    _MAXIMUM_RELATIVE_FOCAL_STDDEV,
    _MAXIMUM_RMS_REPROJECTION_ERROR_PX,
    _MINIMUM_POSE_CONSTRAINT_RATIO,
    _pipeline,
    _pose_constraint_ratio,
)
from calibration.tag_modules import ModuleCorners, extract_module_corners
from perception.tag_localization import tag_corners

_MINIMUM_VIEWS = 20
_MINIMUM_FISHEYE_VIEWS = 25
_MINIMUM_EDGE_PX = 60.0
_MAXIMUM_FISHEYE_HELDOUT_RMS_PX = 0.5
_MAXIMUM_FISHEYE_FOCAL_DRIFT = 0.05
_MAXIMUM_FISHEYE_PRINCIPAL_DRIFT = 0.02
_MAXIMUM_FISHEYE_DISTORTION_DRIFT = 0.2
_GRID_COLUMNS = 4
_GRID_ROWS = 3


@dataclass(frozen=True, slots=True)
class TagCandidateRequest:
    evidence: Path
    tag_size_m: float
    pipeline: dict[str, object]
    minimum_frame_gap: int = 8
    maximum_views: int = 30
    model: str = "pinhole"
    frames_dir: Path | None = None


def export_tag_calibration(
    request: TagCandidateRequest,
    *,
    camera_serial: str,
    evidence_kind: str,
    allow_synthetic: bool = False,
) -> dict[str, object]:
    if not camera_serial.strip():
        raise ValueError("camera serial must not be empty")
    if evidence_kind not in {"recorded_live", "synthetic"} or (
        evidence_kind == "synthetic" and not allow_synthetic
    ):
        raise ValueError("recorded live evidence is required; synthetic export must be explicit")
    document = _evidence_document(request.evidence)
    candidate = calibrate_tag_candidate(request)
    if candidate["status"] != "candidate":
        raise ValueError("AprilTag candidate did not meet calibration quality requirements")
    selected = candidate["selection"]
    if not isinstance(selected, dict) or not isinstance(selected.get("frames"), list):
        raise ValueError("candidate selection is invalid")
    frames = selected["frames"]
    if len(frames) != candidate["selected_observation_count"] or len(set(frames)) != len(frames):
        raise ValueError("candidate observations do not map to distinct source images")
    if request.frames_dir is None:
        raise ValueError("AprilTag export requires the decoded source images")
    hashes = {}
    for index in frames:
        if type(index) is not int:
            raise ValueError("candidate frame index is invalid")
        image = request.frames_dir / f"frame-{index:06}.png"
        decoded = cv2.imread(str(image))
        if decoded is None or [decoded.shape[1], decoded.shape[0]] != candidate["image_size_px"]:
            raise ValueError(f"missing or mismatched source image: {image}")
        hashes[image.name] = hashlib.sha256(image.read_bytes()).hexdigest()
    count = len(hashes)
    if count < _MINIMUM_VIEWS:
        raise ValueError("fewer than 20 distinct source images")
    if len(set(hashes.values())) != count:
        raise ValueError("source images are not distinct")
    if evidence_kind == "recorded_live":
        _validate_recorded_live_provenance(document, request, frames, hashes)
    model = candidate["model"]
    artifact = {
        "schema_version": 2 if model == "fisheye" else 1,
        "model": model,
        "status": "offline",
        "evidence_kind": evidence_kind,
        "camera_serial": camera_serial,
        "pipeline": candidate["pipeline"],
        "target": {
            "family": "tag36h11",
            "black_square_edge_m": request.tag_size_m,
            "validated_feature_kind": (
                "outer_corners_plus_module_intersections" if model == "fisheye" else "outer_corners"
            ),
        },
        "image_size_px": candidate["image_size_px"],
        "camera_matrix": candidate["camera_matrix"],
        "distortion_coefficients": candidate["distortion_coefficients"],
        "rms_reprojection_error_px": candidate["rms_reprojection_error_px"],
        "accepted_image_count": count,
        "image_sha256": hashes,
        "tag_candidate_quality": candidate.get("quality"),
    }
    if model == "fisheye":
        quality = candidate.get("quality")
        if not isinstance(quality, dict):
            raise ValueError("fisheye candidate quality is missing")
        artifact["quality"] = {
            "accepted_image_count": count,
            "minimum_accepted_image_count": _MINIMUM_VIEWS,
            "rms_reprojection_error_px": candidate["rms_reprojection_error_px"],
            "maximum_rms_reprojection_error_px": _MAXIMUM_RMS_REPROJECTION_ERROR_PX,
            "minimum_pose_constraint_ratio": _MINIMUM_POSE_CONSTRAINT_RATIO,
            "pose_constraint_ratio": candidate["pose_constraint_ratio"],
            "opencv_check_cond": True,
            "heldout_rms_reprojection_error_px": quality["heldout_rms_reprojection_error_px"],
            "parameter_stability": quality["parameter_stability"],
            "fisheye_fov_deg": candidate["fisheye_fov_deg"],
        }
    return artifact


def _evidence_document(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read tag evidence: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("frames"), list):
        raise ValueError("tag evidence must contain a frames array")
    return document


def _validate_recorded_live_provenance(
    document: dict[str, object],
    request: TagCandidateRequest,
    selected: list[object],
    hashes: dict[str, str],
) -> None:
    provenance = document.get("recorded_live_provenance")
    if not isinstance(provenance, dict) or request.frames_dir is None:
        raise ValueError("recorded live export requires capture provenance")
    if "session_file" in provenance or "session_sha256" in provenance:
        _validate_recorded_live_session(document, request, selected, hashes, provenance)
        return
    _validate_single_recorded_live_provenance(document, request, selected, hashes)


def _validate_single_recorded_live_provenance(
    document: dict[str, object],
    request: TagCandidateRequest,
    selected: list[object],
    hashes: dict[str, str],
) -> None:
    provenance = document.get("recorded_live_provenance")
    if not isinstance(provenance, dict) or request.frames_dir is None:
        raise ValueError("recorded live export requires capture provenance")
    manifest_name = provenance.get("manifest_file")
    manifest_hash = provenance.get("manifest_sha256")
    stream = provenance.get("stream_id")
    if (
        not isinstance(manifest_name, str)
        or Path(manifest_name).name != manifest_name
        or not isinstance(manifest_hash, str)
        or not isinstance(stream, str)
    ):
        raise ValueError("recorded live provenance is invalid")
    manifest_path = request.evidence.parent / manifest_name
    if not manifest_path.is_file() or _sha256(manifest_path) != manifest_hash:
        raise ValueError("capture manifest is missing or changed")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError("capture manifest is invalid")
    cameras = manifest.get("cameras")
    if (
        manifest.get("status") != "complete"
        or not isinstance(cameras, dict)
        or stream not in cameras
    ):
        raise ValueError("capture manifest is incomplete or stream is absent")
    collection = manifest.get("raw_capture_collection")
    boot = manifest.get("boot_id")
    pipeline_hash = manifest.get("capture_pipeline_sha256")
    pipeline = manifest.get("capture_pipeline")
    if (
        manifest.get("schema_version") != "ohmni-dual-calibration-capture/v2"
        or not all(
            isinstance(value, str) and value.strip()
            for value in (collection, boot, manifest.get("serial"))
        )
        or not isinstance(pipeline, dict)
        or pipeline != cameras
        or hashlib.sha256(
            json.dumps(pipeline, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        != pipeline_hash
    ):
        raise ValueError("capture manifest lacks a valid collection or pipeline binding")
    if request.pipeline.get("capture_pipeline_sha256") != pipeline_hash:
        raise ValueError("requested pipeline is not bound to the capture manifest")
    stream_pipeline = pipeline[stream]
    if not isinstance(stream_pipeline, dict):
        raise ValueError("capture stream pipeline is invalid")
    recorded_pipeline = stream_pipeline.get("calibration_pipeline")
    if not isinstance(recorded_pipeline, dict) or any(
        request.pipeline.get(key) != recorded_pipeline.get(key) or not recorded_pipeline.get(key)
        for key in (
            "resolution_px",
            "codec",
            "decoder_path",
            "camera_mode",
            "camera_identity",
            "android_device_id",
            "network_id",
        )
    ):
        raise ValueError("requested pipeline differs from the recorded camera pipeline")
    expected_modes = {
        "main": ("/dev/video0", "UYVY", [1280, 720], "raw UYVY", "2560", "c1d1", "See3CAM_CU135"),
        "lower": ("/dev/video1", "MJPG", [640, 480], "MJPEG", "32e4", "9230", "HD USB Camera"),
    }
    mode = expected_modes.get(stream)
    if mode is None:
        raise ValueError("unsupported recorded camera stream")
    device, pixel_format, shape, codec, vendor, product, name = mode
    identity = f"{vendor}:{product} {name}"
    if stream_pipeline.get("usb_serial"):
        identity += f" serial={stream_pipeline['usb_serial']}"
    if (
        any(
            stream_pipeline.get(key) != value
            for key, value in (
                ("device", device),
                ("pixel_format", pixel_format),
                ("shape_px", shape),
                ("usb_vendor_id", vendor),
                ("usb_product_id", product),
                ("device_name", name),
            )
        )
        or not isinstance(stream_pipeline.get("usb_parent"), str)
        or not stream_pipeline["usb_parent"].startswith("/")
        or recorded_pipeline
        != {
            "resolution_px": shape,
            "codec": codec,
            "decoder_path": "tools.ohmni_dual_calibration_capture._decode",
            "camera_mode": f"{device} {shape[0]}x{shape[1]}",
            "camera_identity": identity,
            "android_device_id": manifest["serial"],
            "network_id": "ADB",
        }
    ):
        raise ValueError("recorded camera identity or decode mode is unsupported")
    source_frames = {}
    for item in document["frames"]:
        if not isinstance(item, dict) or type(item.get("frame_index")) is not int:
            raise ValueError("source frame index is invalid")
        index = item["frame_index"]
        if index in source_frames:
            raise ValueError("source frame indices are duplicated")
        source_frames[index] = item
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    for index in selected:
        frame = source_frames.get(index)
        if (
            not isinstance(frame, dict)
            or frame.get("boot_id") != boot
            or frame.get("camera") != stream
            or frame.get("shape_px") != shape
            or frame.get("raw_capture_collection") != collection
            or frame.get("capture_pipeline_sha256") != pipeline_hash
        ):
            raise ValueError("selected frame is not bound to the capture boot and stream")
        image_name, raw_name = frame.get("image_file"), frame.get("source_file")
        image_hash, raw_hash = frame.get("image_sha256"), frame.get("source_sha256")
        if not all(
            isinstance(value, str) and value
            for value in (image_name, raw_name, image_hash, raw_hash)
        ):
            raise ValueError("selected frame lacks raw and raster hashes")
        if (
            image_name != f"frame-{index:06}.png"
            or Path(raw_name).name != raw_name
            or raw_name in {".", ".."}
        ):
            raise ValueError("selected frame paths are unsafe or mismatched")
        image = request.frames_dir / image_name
        raw = request.frames_dir / raw_name
        if not raw.is_file() or hashes.get(image_name) != image_hash or _sha256(raw) != raw_hash:
            raise ValueError("selected raster or raw source changed")
        if (
            frame.get("source_device_sha256") != raw_hash
            or type(frame.get("source_device_size_bytes")) is not int
            or frame["source_device_size_bytes"] != raw.stat().st_size
        ):
            raise ValueError("selected raw source differs from the device fingerprint")
        packed = np.frombuffer(raw.read_bytes(), dtype=np.uint8)
        if pixel_format == "UYVY":
            if packed.size != shape[0] * shape[1] * 2:
                raise ValueError("selected raw UYVY source has an invalid size")
            raster = cv2.cvtColor(packed.reshape(shape[1], shape[0], 2), cv2.COLOR_YUV2BGR_UYVY)
        else:
            raster = cv2.imdecode(packed, cv2.IMREAD_COLOR)
        decoded = cv2.imread(str(image))
        if raster is None or not np.array_equal(raster, decoded):
            raise ValueError("selected raster does not decode from the retained raw source")
        corners, identifiers, _ = detector.detectMarkers(decoded)
        observed = (
            {}
            if identifiers is None
            else {
                int(tag): corner.reshape(4, 2)
                for tag, corner in zip(identifiers.reshape(-1), corners, strict=True)
            }
        )
        tags, pixels = frame.get("tag_ids"), frame.get("corners_px")
        if (
            not isinstance(tags, list)
            or not isinstance(pixels, list)
            or not tags
            or len(tags) != len(pixels)
            or any(type(tag) is not int for tag in tags)
            or len(set(tags)) != len(tags)
        ):
            raise ValueError("selected tag corners are invalid")
        for tag, points in zip(tags, pixels, strict=True):
            points = np.asarray(points, dtype=float)
            if (
                tag not in observed
                or points.shape != (4, 2)
                or not np.array_equal(observed[tag], points)
            ):
                raise ValueError("selected tag corners do not match the hashed raster")


def _validate_recorded_live_session(
    document: dict[str, object],
    request: TagCandidateRequest,
    selected: list[object],
    hashes: dict[str, str],
    provenance: dict[str, object],
) -> None:
    assert request.frames_dir is not None
    session_name = provenance.get("session_file")
    session_hash = provenance.get("session_sha256")
    stream = provenance.get("stream_id")
    if (
        not isinstance(session_name, str)
        or Path(session_name).name != session_name
        or not isinstance(session_hash, str)
        or not isinstance(stream, str)
    ):
        raise ValueError("recorded live session provenance is invalid")
    session_path = request.evidence.parent / session_name
    if not session_path.is_file() or _sha256(session_path) != session_hash:
        raise ValueError("calibration session is missing or changed")
    try:
        session = json.loads(session_path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError("calibration session is invalid") from error
    if (
        not isinstance(session, dict)
        or session.get("schema_version") != "ohmni-calibration-session/v1"
        or session.get("status") != "complete"
        or session.get("stream_id") != stream
        or not isinstance(session.get("camera_pipeline"), dict)
        or not isinstance(session.get("sources"), list)
        or not isinstance(session.get("frames"), list)
    ):
        raise ValueError("calibration session is invalid")
    sources = _session_sources(session, session_path.parent, stream)
    aggregate = _session_frame_bindings(session, sources)
    document_frames: dict[int, dict[str, object]] = {}
    for frame in document["frames"]:
        if not isinstance(frame, dict) or type(frame.get("frame_index")) is not int:
            raise ValueError("aggregate frame index is invalid")
        index = frame["frame_index"]
        if index in document_frames:
            raise ValueError("aggregate frame indexes are duplicated")
        document_frames[index] = frame
    groups: dict[int, list[tuple[int, dict[str, object], dict[str, object]]]] = {}
    for index in selected:
        if type(index) is not int:
            raise ValueError("candidate frame index is invalid")
        frame = document_frames.get(index)
        binding = aggregate.get(index)
        if frame is None or binding is None:
            raise ValueError("selected frame is absent from the calibration session")
        source_index = binding["source_session_index"]
        source = sources.get(source_index)
        if source is None:
            raise ValueError("aggregate frame has an unknown source")
        _validate_aggregate_frame(
            frame, binding, source, request.frames_dir, hashes, session_path.parent
        )
        groups.setdefault(source_index, []).append((index, frame, binding))
    for source_index, frames in groups.items():
        source = sources[source_index]
        source_document = source["result"]
        source_request = TagCandidateRequest(
            evidence=source["result_path"],
            tag_size_m=request.tag_size_m,
            pipeline=request.pipeline,
            minimum_frame_gap=request.minimum_frame_gap,
            maximum_views=request.maximum_views,
            model=request.model,
            frames_dir=source["directory"],
        )
        source_selected = [binding["source_frame_index"] for _, _, binding in frames]
        source_frames = {
            item["frame_index"]: item
            for item in source_document.get("frames", [])
            if isinstance(item, dict) and type(item.get("frame_index")) is int
        }
        source_hashes = {}
        for source_index in source_selected:
            source_frame = source_frames.get(source_index)
            if not isinstance(source_frame, dict) or not isinstance(
                source_frame.get("image_file"), str
            ):
                raise ValueError("selected source frame is invalid")
            image_name = source_frame["image_file"]
            source_hashes[image_name] = _sha256(source["directory"] / image_name)
        _validate_single_recorded_live_provenance(
            source_document, source_request, source_selected, source_hashes
        )


def _session_sources(
    session: dict[str, object], root: Path, stream: str
) -> dict[int, dict[str, object]]:
    expected_pipeline = session["camera_pipeline"]
    assert isinstance(expected_pipeline, dict)
    sources: dict[int, dict[str, object]] = {}
    session_sources = session["sources"]
    assert isinstance(session_sources, list)
    if len(session_sources) < 2:
        raise ValueError("calibration session has fewer than two sources")
    collections: set[str] = set()
    for entry in session_sources:
        if not isinstance(entry, dict) or type(entry.get("source_session_index")) is not int:
            raise ValueError("calibration session source is invalid")
        index = entry["source_session_index"]
        if index in sources:
            raise ValueError("calibration session source indexes are duplicated")
        collection = entry.get("raw_capture_collection")
        if not isinstance(collection, str) or not collection or collection in collections:
            raise ValueError("calibration session source collections are invalid")
        collections.add(collection)
        if entry.get("stream_id") != stream or entry.get("camera_pipeline") != expected_pipeline:
            raise ValueError("calibration session camera pipelines differ")
        directory = _session_path(root, entry.get("result_file"), "source result file").parent
        result_path = _session_path(root, entry.get("result_file"), "source result file")
        manifest_path = _session_path(root, entry.get("manifest_file"), "source manifest file")
        result_hash, manifest_hash = entry.get("result_sha256"), entry.get("manifest_sha256")
        if (
            not result_path.is_file()
            or not manifest_path.is_file()
            or not isinstance(result_hash, str)
            or not isinstance(manifest_hash, str)
            or _sha256(result_path) != result_hash
            or _sha256(manifest_path) != manifest_hash
        ):
            raise ValueError("calibration session source hashes changed")
        files = entry.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("calibration session source files are invalid")
        for name, digest in files.items():
            path = _session_path(root, name, "source file")
            if not isinstance(digest, str) or not path.is_file() or _sha256(path) != digest:
                raise ValueError("calibration session source file changed")
        try:
            result = json.loads(result_path.read_text())
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError as error:
            raise ValueError("calibration session source document is invalid") from error
        if not isinstance(result, dict) or not isinstance(manifest, dict):
            raise ValueError("calibration session source document is invalid")
        cameras = manifest.get("capture_pipeline")
        if (
            not isinstance(cameras, dict)
            or not isinstance(cameras.get(stream), dict)
            or cameras[stream] != expected_pipeline
            or entry.get("boot_id") != manifest.get("boot_id")
            or entry.get("raw_capture_collection") != manifest.get("raw_capture_collection")
            or entry.get("capture_pipeline_sha256") != manifest.get("capture_pipeline_sha256")
        ):
            raise ValueError("calibration session source binding is invalid")
        frames = entry.get("frames")
        if not isinstance(frames, list) or not frames:
            raise ValueError("calibration session source frame bindings are invalid")
        sources[index] = {
            "entry": entry,
            "directory": directory,
            "result_path": result_path,
            "result": result,
            "frames": frames,
        }
    if set(sources) != set(range(len(sources))):
        raise ValueError("calibration session source indexes are invalid")
    return sources


def _session_frame_bindings(
    session: dict[str, object], sources: dict[int, dict[str, object]]
) -> dict[int, dict[str, object]]:
    bindings: dict[int, dict[str, object]] = {}
    source_mappings: set[tuple[int, int]] = set()
    frames = session["frames"]
    assert isinstance(frames, list)
    for binding in frames:
        if (
            not isinstance(binding, dict)
            or type(binding.get("frame_index")) is not int
            or type(binding.get("source_session_index")) is not int
            or type(binding.get("source_frame_index")) is not int
        ):
            raise ValueError("calibration session frame binding is invalid")
        index = binding["frame_index"]
        source_index = binding["source_session_index"]
        if index in bindings or source_index not in sources:
            raise ValueError("calibration session frame mapping is invalid")
        source_mapping = (source_index, binding["source_frame_index"])
        if source_mapping in source_mappings:
            raise ValueError("calibration session frame mapping is duplicated")
        source_mappings.add(source_mapping)
        source_bindings = sources[source_index]["frames"]
        assert isinstance(source_bindings, list)
        source_frame = next(
            (
                item
                for item in source_bindings
                if isinstance(item, dict)
                and item.get("source_frame_index") == binding["source_frame_index"]
            ),
            None,
        )
        if source_frame is None:
            raise ValueError("calibration session frame has no source binding")
        for key in ("image_file", "image_sha256", "source_file", "source_sha256"):
            if not isinstance(binding.get(key), str):
                raise ValueError("calibration session aggregate file binding is invalid")
        bindings[index] = binding
    if not bindings:
        raise ValueError("calibration session has no frame bindings")
    return bindings


def _session_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"{label} is unsafe")
    path = root / relative
    if root not in path.parents and path != root:
        raise ValueError(f"{label} is unsafe")
    return path


def _validate_aggregate_frame(
    frame: dict[str, object],
    binding: dict[str, object],
    source: dict[str, object],
    frames_dir: Path,
    hashes: dict[str, str],
    session_root: Path,
) -> None:
    source_frames = source["result"].get("frames")
    assert isinstance(source_frames, list)
    original = next(
        (
            item
            for item in source_frames
            if isinstance(item, dict) and item.get("frame_index") == binding["source_frame_index"]
        ),
        None,
    )
    if original is None:
        raise ValueError("aggregate frame source is absent from its result")
    aggregate_index = binding["frame_index"]
    original_raw = original.get("source_file")
    if not isinstance(aggregate_index, int) or not isinstance(original_raw, str):
        raise ValueError("aggregate frame mapping is invalid")
    canonical_image = f"frame-{aggregate_index:06}.png"
    canonical_raw = f"raw-{aggregate_index:06}{Path(original_raw).suffix}"
    if binding.get("image_file") != canonical_image or binding.get("source_file") != canonical_raw:
        raise ValueError("aggregate frame filenames are invalid")
    expected = dict(original)
    expected.update(
        frame_index=aggregate_index,
        image_file=canonical_image,
        source_file=canonical_raw,
        source_session_index=binding["source_session_index"],
        source_frame_index=binding["source_frame_index"],
    )
    for key in ("image_sha256", "source_sha256"):
        expected[key] = binding[key]
    if frame != expected:
        raise ValueError("aggregate frame does not match its source frame")
    image_name = frame["image_file"]
    raw_name = frame["source_file"]
    assert isinstance(image_name, str) and isinstance(raw_name, str)
    image = frames_dir / image_name
    raw = frames_dir / raw_name
    if (
        not image.is_file()
        or not raw.is_file()
        or hashes.get(image_name) != frame.get("image_sha256")
        or _sha256(raw) != frame.get("source_sha256")
    ):
        raise ValueError("aggregate raster or raw source changed")
    source_frame = next(
        (
            item
            for item in source["frames"]
            if isinstance(item, dict)
            and item.get("source_frame_index") == binding["source_frame_index"]
        ),
        None,
    )
    if not isinstance(source_frame, dict):
        raise ValueError("aggregate frame source binding is invalid")
    source_image = _session_path(session_root, source_frame.get("image_file"), "source image file")
    source_raw = _session_path(session_root, source_frame.get("source_file"), "source raw file")
    if (
        _sha256(source_image) != frame["image_sha256"]
        or _sha256(source_raw) != frame["source_sha256"]
    ):
        raise ValueError("aggregate files differ from their retained source")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def calibrate_tag_candidate(request: TagCandidateRequest) -> dict[str, object]:
    """Fit a diagnostic candidate; this never creates a usable calibration artifact."""
    if not isfinite(request.tag_size_m) or request.tag_size_m <= 0:
        raise ValueError("tag size must be a positive number of meters")
    if request.model not in {"pinhole", "fisheye"}:
        raise ValueError("model must be pinhole or fisheye")
    if request.minimum_frame_gap < 1 or request.maximum_views < _MINIMUM_VIEWS:
        raise ValueError("minimum frame gap must be positive and maximum views must be at least 20")
    pipeline = _pipeline(request.pipeline)
    try:
        document = json.loads(request.evidence.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read tag evidence: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("frames"), list):
        raise ValueError("tag evidence must contain a frames array")

    image_size, observations = _observations(document["frames"])
    if pipeline["resolution_px"] != list(image_size):
        raise ValueError("pipeline resolution_px does not match tag evidence")
    selected = _select(observations, request.minimum_frame_gap, request.maximum_views)
    module_views: list[tuple[int, int, float, ModuleCorners]] | None = None
    if request.model == "fisheye" and request.frames_dir is not None:
        module_views = _select_module_views(
            _module_candidates(observations, request.frames_dir, request.tag_size_m, image_size),
            request.minimum_frame_gap,
            request.maximum_views,
            image_size,
        )
        selected = [
            (index, identifier, module.image_points[:4])
            for index, identifier, _, module in module_views
        ]
    minimum_views = _MINIMUM_FISHEYE_VIEWS if request.model == "fisheye" else _MINIMUM_VIEWS
    report: dict[str, object] = {
        "kind": "apriltag_intrinsics_candidate",
        "status": "rejected",
        "source_evidence": str(request.evidence),
        "pipeline": pipeline,
        "image_size_px": list(image_size),
        "tag_black_square_edge_m": request.tag_size_m,
        "model": request.model,
        "raw_observation_count": len(observations),
        "selected_observation_count": len(selected),
        "minimum_selected_observation_count": minimum_views,
        "minimum_shortest_edge_px": _MINIMUM_EDGE_PX,
        "selection": {
            "minimum_frame_gap": request.minimum_frame_gap,
            "maximum_views": request.maximum_views,
            "frames": [item[0] for item in selected],
            "tag_ids": [item[1] for item in selected],
        },
        "rejection_reasons": [],
    }
    reasons: list[str] = report["rejection_reasons"]  # type: ignore[assignment]
    if len(selected) < minimum_views:
        reasons.append(
            "fewer than 25 frames have six validated observed tag corners"
            if request.model == "fisheye"
            else "fewer than 20 separated raw four-corner observations"
        )
        return report

    image_points = [pixels.astype(np.float32) for _, _, pixels in selected]
    object_points = [tag_corners(request.tag_size_m).astype(np.float32) for _ in selected]
    normalized = [
        (points - np.asarray(image_size, dtype=float) / 2) / max(image_size)
        for points in image_points
    ]
    ratio = _pose_constraint_ratio(object_points[0], normalized)
    report["pose_constraint_ratio"] = ratio
    report["minimum_pose_constraint_ratio"] = _MINIMUM_POSE_CONSTRAINT_RATIO
    if ratio < _MINIMUM_POSE_CONSTRAINT_RATIO:
        reasons.append("square homographies are insufficiently varied")
        return report

    if request.model == "fisheye":
        if request.frames_dir is None:
            reasons.append("fisheye fitting requires --frames-dir with the raw images")
            return report
        assert module_views is not None
        objects = [
            view[3].object_points.reshape(-1, 1, 3).astype(np.float64) for view in module_views
        ]
        pixels = [
            view[3].image_points.reshape(-1, 1, 2).astype(np.float64) for view in module_views
        ]
        try:
            rms, camera_matrix, distortion = _fit_fisheye(objects, pixels, image_size)
            train_indices = [index for index in range(len(objects)) if index % 5]
            heldout_indices = [index for index in range(len(objects)) if not index % 5]
            train_rms, train_matrix, train_distortion = _fit_fisheye(
                [objects[index] for index in train_indices],
                [pixels[index] for index in train_indices],
                image_size,
            )
            heldout_rms = _fisheye_heldout_rms(
                [objects[index] for index in heldout_indices],
                [pixels[index] for index in heldout_indices],
                train_matrix,
                train_distortion,
            )
        except cv2.error:
            reasons.append("fisheye calibration is ill-conditioned")
            return report
        stability = _fisheye_stability(
            camera_matrix, distortion, train_matrix, train_distortion, image_size
        )
        fov = _fisheye_fov(camera_matrix, distortion, image_size)
        report.update(
            rms_reprojection_error_px=float(rms),
            camera_matrix=camera_matrix.tolist(),
            distortion_coefficients=distortion.reshape(-1).tolist(),
            validated_module_view_count=len(module_views),
            fisheye_fov_deg=fov,
            quality={
                "fit_rms_reprojection_error_px": float(rms),
                "training_rms_reprojection_error_px": float(train_rms),
                "heldout_rms_reprojection_error_px": heldout_rms,
                "maximum_heldout_rms_reprojection_error_px": _MAXIMUM_FISHEYE_HELDOUT_RMS_PX,
                "parameter_stability": stability,
            },
        )
        if not isfinite(float(rms)) or rms >= _MAXIMUM_RMS_REPROJECTION_ERROR_PX:
            reasons.append("RMS reprojection error is at least 0.5 pixels")
        if not isfinite(heldout_rms) or heldout_rms >= _MAXIMUM_FISHEYE_HELDOUT_RMS_PX:
            reasons.append("held-out RMS reprojection error is at least 0.5 pixels")
        if not stability["passes"]:
            reasons.append("fisheye parameters are unstable after withholding views")
        bounds = pipeline.get("fov_bounds_deg")
        if not _fov_within_bounds(fov, bounds):
            reasons.append("estimated fisheye FOV lacks valid independent bounds")
        if not reasons:
            report["status"] = "candidate"
        return report
    rms, camera_matrix, distortion, _, _, stddev, _, _ = cv2.calibrateCameraExtended(
        object_points, image_points, image_size, None, None
    )
    report["rms_reprojection_error_px"] = float(rms)
    report["camera_matrix"] = camera_matrix.tolist()
    report["distortion_coefficients"] = distortion.reshape(-1).tolist()
    focal = np.array([camera_matrix[0, 0], camera_matrix[1, 1]])
    focal_stddev = stddev.reshape(-1)[:2]
    relative = focal_stddev / focal
    report["focal_stddev_px"] = focal_stddev.tolist()
    report["relative_focal_stddev"] = relative.tolist()
    fov = _fov(camera_matrix, image_size)
    report["pinhole_fov_deg"] = fov
    if not isfinite(float(rms)) or rms >= _MAXIMUM_RMS_REPROJECTION_ERROR_PX:
        reasons.append("RMS reprojection error is at least 0.5 pixels")
    if (
        focal_stddev.shape != (2,)
        or not np.isfinite(focal_stddev).all()
        or np.any(focal_stddev <= 0)
        or np.any(relative > _MAXIMUM_RELATIVE_FOCAL_STDDEV)
    ):
        reasons.append("focal length uncertainty exceeds 5 percent")
    bounds = pipeline.get("fov_bounds_deg")
    if not isinstance(bounds, dict):
        reasons.append("pipeline omits independent FOV bounds")
    else:
        for axis in ("horizontal", "vertical"):
            interval = bounds.get(axis)
            if not isinstance(interval, list) or not interval[0] <= fov[axis] <= interval[1]:
                reasons.append(f"estimated {axis} FOV is outside declared bounds")
    if not reasons:
        report["status"] = "candidate"
    return report


def _observations(
    frames: list[object],
) -> tuple[tuple[int, int], list[tuple[int, int, np.ndarray]]]:
    image_size: tuple[int, int] | None = None
    observations: list[tuple[int, int, np.ndarray]] = []
    for raw in frames:
        if not isinstance(raw, dict):
            continue
        shape, identifiers, corners = raw.get("shape_px"), raw.get("tag_ids"), raw.get("corners_px")
        index = raw.get("frame_index")
        if (
            not isinstance(shape, list)
            or len(shape) != 2
            or any(type(value) is not int or value <= 0 for value in shape)
            or type(index) is not int
            or not isinstance(identifiers, list)
            or not isinstance(corners, list)
            or len(identifiers) != len(corners)
        ):
            continue
        size = (shape[0], shape[1])
        if image_size is None:
            image_size = size
        elif size != image_size:
            raise ValueError("tag evidence contains more than one image resolution")
        for identifier, corner_set in zip(identifiers, corners, strict=True):
            pixels = np.asarray(corner_set, dtype=float)
            if (
                type(identifier) is not int
                or pixels.shape != (4, 2)
                or not np.isfinite(pixels).all()
            ):
                continue
            edges = np.linalg.norm(pixels - np.roll(pixels, 1, axis=0), axis=1)
            if np.min(edges) >= _MINIMUM_EDGE_PX:
                observations.append((index, identifier, pixels))
    if image_size is None:
        raise ValueError("tag evidence contains no decoded frames")
    return image_size, observations


def _select(
    observations: list[tuple[int, int, np.ndarray]], minimum_frame_gap: int, maximum_views: int
) -> list[tuple[int, int, np.ndarray]]:
    eligible: list[tuple[int, int, np.ndarray]] = []
    for item in observations:
        if all(abs(item[0] - prior[0]) >= minimum_frame_gap for prior in eligible):
            eligible.append(item)
    return _temporal_sample(eligible, maximum_views)


def _module_candidates(
    observations: list[tuple[int, int, np.ndarray]],
    frames_dir: Path,
    tag_size_m: float,
    image_size: tuple[int, int],
) -> list[tuple[int, int, float, ModuleCorners]]:
    if not frames_dir.is_dir():
        raise ValueError("frames directory does not exist")
    candidates = []
    for index, identifier, corners in observations:
        path = frames_dir / f"frame-{index:06}.png"
        image = cv2.imread(str(path))
        if image is None or (image.shape[1], image.shape[0]) != image_size:
            raise ValueError(f"missing or mismatched frame image: {path}")
        module = extract_module_corners(image, identifier, corners, tag_size_m)
        if module is not None:
            edge = float(np.min(np.linalg.norm(corners - np.roll(corners, 1, axis=0), axis=1)))
            candidates.append((index, identifier, edge, module))
    return candidates


def _select_module_views(
    candidates: list[tuple[int, int, float, ModuleCorners]],
    minimum_frame_gap: int,
    maximum_views: int,
    image_size: tuple[int, int],
) -> list[tuple[int, int, float, ModuleCorners]]:
    by_frame: dict[int, list[tuple[int, int, float, ModuleCorners]]] = {}
    for candidate in candidates:
        by_frame.setdefault(candidate[0], []).append(candidate)
    covered = np.zeros((_GRID_ROWS, _GRID_COLUMNS), dtype=int)
    eligible = []
    for index, views in sorted(by_frame.items()):
        if not all(abs(index - prior[0]) >= minimum_frame_gap for prior in eligible):
            continue
        view = min(
            views,
            key=lambda item: (
                covered[_grid_cell(item[3].image_points[:4], image_size)],
                -item[3].internal_count,
                -item[3].pattern_match,
                -item[2],
                item[1],
            ),
        )
        covered[_grid_cell(view[3].image_points[:4], image_size)] += 1
        eligible.append(view)
    return _temporal_sample(eligible, maximum_views)


def _temporal_sample[T](items: list[T], maximum_views: int) -> list[T]:
    if len(items) <= maximum_views:
        return items
    indices = np.linspace(0, len(items) - 1, maximum_views, dtype=int)
    return [items[index] for index in indices]


def _grid_cell(points: np.ndarray, image_size: tuple[int, int]) -> tuple[int, int]:
    center = np.mean(points, axis=0)
    column = min(_GRID_COLUMNS - 1, max(0, int(center[0] * _GRID_COLUMNS / image_size[0])))
    row = min(_GRID_ROWS - 1, max(0, int(center[1] * _GRID_ROWS / image_size[1])))
    return row, column


def _fit_fisheye(
    objects: list[np.ndarray], pixels: list[np.ndarray], image_size: tuple[int, int]
) -> tuple[float, np.ndarray, np.ndarray]:
    rms, camera_matrix, distortion, _, _ = cv2.fisheye.calibrate(
        objects,
        pixels,
        image_size,
        None,
        None,
        flags=_FISHEYE_CALIBRATION_FLAGS,
        criteria=_FISHEYE_CRITERIA,
    )
    return float(rms), camera_matrix, distortion


def _fisheye_heldout_rms(
    objects: list[np.ndarray],
    pixels: list[np.ndarray],
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> float:
    squared_error = 0.0
    point_count = 0
    for object_points, observed in zip(objects, pixels, strict=True):
        undistorted = cv2.fisheye.undistortPoints(observed, camera_matrix, distortion)
        solved, rotation, translation = cv2.solvePnP(
            object_points,
            undistorted,
            np.eye(3),
            None,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )
        if not solved:
            return float("inf")
        projected, _ = cv2.fisheye.projectPoints(
            object_points, rotation, translation, camera_matrix, distortion
        )
        residual = projected.reshape(-1, 2) - observed.reshape(-1, 2)
        squared_error += float(np.sum(residual * residual))
        point_count += len(residual)
    return float(np.sqrt(squared_error / point_count)) if point_count else float("inf")


def _fisheye_stability(
    full_matrix: np.ndarray,
    full_distortion: np.ndarray,
    train_matrix: np.ndarray,
    train_distortion: np.ndarray,
    image_size: tuple[int, int],
) -> dict[str, float | bool]:
    full_focal = full_matrix.diagonal()[:2]
    focal_drift = float(np.max(np.abs(train_matrix.diagonal()[:2] - full_focal) / full_focal))
    principal_drift = float(
        np.max(np.abs(train_matrix[:2, 2] - full_matrix[:2, 2]) / np.asarray(image_size))
    )
    distortion_drift = float(
        np.max(
            np.abs(train_distortion.reshape(-1) - full_distortion.reshape(-1))
            / np.maximum(np.abs(full_distortion.reshape(-1)), 0.01)
        )
    )
    passes = (
        np.isfinite([focal_drift, principal_drift, distortion_drift]).all()
        and focal_drift <= _MAXIMUM_FISHEYE_FOCAL_DRIFT
        and principal_drift <= _MAXIMUM_FISHEYE_PRINCIPAL_DRIFT
        and distortion_drift <= _MAXIMUM_FISHEYE_DISTORTION_DRIFT
    )
    return {
        "focal_relative_drift": focal_drift,
        "principal_point_relative_drift": principal_drift,
        "distortion_relative_drift": distortion_drift,
        "maximum_focal_relative_drift": _MAXIMUM_FISHEYE_FOCAL_DRIFT,
        "maximum_principal_point_relative_drift": _MAXIMUM_FISHEYE_PRINCIPAL_DRIFT,
        "maximum_distortion_relative_drift": _MAXIMUM_FISHEYE_DISTORTION_DRIFT,
        "passes": bool(passes),
    }


def _fisheye_fov(
    camera_matrix: np.ndarray, distortion: np.ndarray, image_size: tuple[int, int]
) -> dict[str, float]:
    width, height = image_size

    def ray(point: tuple[float, float]) -> np.ndarray:
        normalized = cv2.fisheye.undistortPoints(
            np.asarray(point, dtype=np.float64).reshape(1, 1, 2), camera_matrix, distortion
        ).reshape(2)
        return np.array([normalized[0], normalized[1], 1.0])

    def angle(first: np.ndarray, second: np.ndarray) -> float:
        cosine = np.dot(first, second) / (np.linalg.norm(first) * np.linalg.norm(second))
        return degrees(acos(float(np.clip(cosine, -1, 1))))

    return {
        "horizontal": angle(ray((0, height / 2)), ray((width, height / 2))),
        "vertical": angle(ray((width / 2, 0)), ray((width / 2, height))),
    }


def _fov_within_bounds(fov: dict[str, float], bounds: object) -> bool:
    if not isinstance(bounds, dict):
        return False
    for axis in ("horizontal", "vertical"):
        interval = bounds.get(axis)
        if (
            not isinstance(interval, list)
            or len(interval) != 2
            or not all(isinstance(value, (int, float)) and isfinite(value) for value in interval)
            or not interval[0] <= fov[axis] <= interval[1]
        ):
            return False
    return True


def _fov(camera_matrix: np.ndarray, image_size: tuple[int, int]) -> dict[str, float]:
    result = {}
    for index, axis in enumerate(("horizontal", "vertical")):
        focal, principal = camera_matrix[index, index], camera_matrix[index, 2]
        result[axis] = degrees(
            atan(principal / focal) + atan((image_size[index] - principal) / focal)
        )
    return result
