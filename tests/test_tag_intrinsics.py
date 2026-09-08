from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from calibration.tag_intrinsics import (
    TagCandidateRequest,
    calibrate_tag_candidate,
    export_tag_calibration,
)
from calibration.tag_modules import ModuleCorners
from perception.camera_tags import CameraTagDetector
from perception.tag_localization import tag_corners
from tools import ohmni_dual_calibration_capture as dual_capture
from tools.ohmni_calibration_session import create_session


def _pipeline() -> dict[str, object]:
    return {
        "resolution_px": [1280, 720],
        "codec": "h264",
        "decoder_path": "fixture",
        "camera_mode": "fpv",
        "android_device_id": "fixture",
        "network_id": "fixture",
        "fov_bounds_deg": {"horizontal": [40, 100], "vertical": [30, 80]},
    }


def _evidence(path: Path, rotations: list[np.ndarray]) -> None:
    camera = np.array([[850.0, 0.0, 640.0], [0.0, 830.0, 360.0], [0.0, 0.0, 1.0]])
    frames = []
    for index, rotation in enumerate(rotations):
        pixels, _ = cv2.projectPoints(
            tag_corners(0.199898),
            rotation,
            np.array([(index % 5 - 2) * 0.08, (index % 4 - 1.5) * 0.06, 1.4 + index * 0.02]),
            camera,
            None,
        )
        frames.append(
            {
                "frame_index": index * 10,
                "shape_px": [1280, 720],
                "tag_ids": [index],
                "corners_px": [pixels.reshape(4, 2).tolist()],
            }
        )
    path.write_text(json.dumps({"frames": frames}))


def test_tag_candidate_recovers_varied_square_intrinsics(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    rotations = [
        np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index]) for index in range(25)
    ]
    _evidence(evidence, rotations)

    result = calibrate_tag_candidate(
        TagCandidateRequest(evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline())
    )

    assert result["status"] == "candidate"
    assert result["selected_observation_count"] == 25
    assert result["camera_matrix"][0][0] == pytest.approx(850.0, rel=0.01)
    assert result["camera_matrix"][1][1] == pytest.approx(830.0, rel=0.01)


def test_tag_candidate_rejects_parallel_square_views(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    _evidence(evidence, [np.zeros(3) for _ in range(25)])

    result = calibrate_tag_candidate(
        TagCandidateRequest(evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline())
    )

    assert result["status"] == "rejected"
    assert "square homographies are insufficiently varied" in result["rejection_reasons"]


def test_tag_candidate_samples_the_full_capture_window(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    _evidence(
        evidence,
        [
            np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index])
            for index in range(31)
        ],
    )

    result = calibrate_tag_candidate(
        TagCandidateRequest(evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline())
    )

    assert result["selected_observation_count"] == 30
    assert result["selection"]["frames"][0] == 0
    assert result["selection"]["frames"][-1] == 300


def test_tag_candidate_refuses_single_square_fisheye_fit(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    _evidence(
        evidence,
        [
            np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index])
            for index in range(25)
        ],
    )

    result = calibrate_tag_candidate(
        TagCandidateRequest(
            evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline(), model="fisheye"
        )
    )

    assert result["status"] == "rejected"
    assert "fisheye fitting requires --frames-dir" in result["rejection_reasons"][0]

    with pytest.raises(ValueError, match="missing or mismatched frame image"):
        calibrate_tag_candidate(
            TagCandidateRequest(
                evidence=evidence,
                tag_size_m=0.199898,
                pipeline=_pipeline(),
                model="fisheye",
                frames_dir=tmp_path,
            )
        )


def test_fisheye_candidate_prevalidates_smaller_module_view_before_selection(
    tmp_path: Path, monkeypatch
) -> None:
    evidence = tmp_path / "corners.json"
    _evidence(
        evidence,
        [
            np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index])
            for index in range(25)
        ],
    )
    document = json.loads(evidence.read_text())
    for frame in document["frames"]:
        large = np.asarray(frame["corners_px"][0], dtype=float)
        center = np.mean(large, axis=0)
        frame["tag_ids"] = [13, 14]
        frame["corners_px"] = [(center + 1.2 * (large - center)).tolist(), large.tolist()]
    evidence.write_text(json.dumps(document))
    frames = tmp_path / "frames"
    frames.mkdir()
    for frame in document["frames"]:
        image = np.full((720, 1280, 3), 255, np.uint8)
        assert cv2.imwrite(str(frames / f"frame-{frame['frame_index']:06}.png"), image)

    def modules(_image, identifier, corners, tag_size_m):
        if identifier == 13:
            return None
        objects = np.vstack([tag_corners(tag_size_m), [[0.0, 0.0, 0.0], [0.03, 0.0, 0.0]]])
        pixels = np.vstack([corners, np.mean(corners, axis=0), np.mean(corners[[0, 1]], axis=0)])
        return ModuleCorners(objects, pixels, internal_count=2, pattern_match=1.0)

    monkeypatch.setattr("calibration.tag_intrinsics.extract_module_corners", modules)
    result = calibrate_tag_candidate(
        TagCandidateRequest(
            evidence=evidence,
            tag_size_m=0.199898,
            pipeline=_pipeline(),
            model="fisheye",
            frames_dir=frames,
        )
    )

    assert result["selected_observation_count"] == 25
    assert result["selection"]["tag_ids"] == [14] * 25
    assert (
        "fewer than 25 frames have six validated observed tag corners"
        not in result["rejection_reasons"]
    )


def test_fisheye_candidate_uses_smaller_valid_tag_from_rendered_raster(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    rotations = [
        np.array([0.18 * np.sin(index), 0.18 * np.cos(index), 0.04 * index]) for index in range(25)
    ]
    _evidence(evidence, rotations)
    document = json.loads(evidence.read_text())
    frames = tmp_path / "frames"
    frames.mkdir()
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    marker = cv2.aruco.generateImageMarker(dictionary, 14, 800)
    source = np.float32([[0, 0], [800, 0], [800, 800], [0, 800]])
    for frame in document["frames"]:
        valid = np.asarray(frame["corners_px"][0], dtype=np.float32)
        center = np.mean(valid, axis=0)
        invalid = center + 1.2 * (valid - center)
        frame["tag_ids"] = [13, 14]
        frame["corners_px"] = [invalid.tolist(), valid.tolist()]
        image = np.full((720, 1280), 255, np.uint8)
        transform = cv2.getPerspectiveTransform(source, valid)
        image = cv2.warpPerspective(
            marker, transform, (1280, 720), dst=image, borderMode=cv2.BORDER_TRANSPARENT
        )
        assert cv2.imwrite(str(frames / f"frame-{frame['frame_index']:06}.png"), image)
    evidence.write_text(json.dumps(document))

    result = calibrate_tag_candidate(
        TagCandidateRequest(
            evidence=evidence,
            tag_size_m=0.199898,
            pipeline=_pipeline(),
            model="fisheye",
            frames_dir=frames,
        )
    )

    assert result["selected_observation_count"] == 25
    assert result["selection"]["tag_ids"] == [14] * 25


def test_fisheye_heldout_reprojection_calls_opencv_and_rejects_perturbed_corners() -> None:
    from calibration.tag_intrinsics import _fisheye_heldout_rms

    camera = np.array([[500.0, 0.0, 640.0], [0.0, 505.0, 360.0], [0.0, 0.0, 1.0]])
    distortion = np.array([[-0.08], [0.01], [0.0], [0.0]])
    objects = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.1, 0.0, 0.0],
            [0.2, 0.0, 0.0],
            [0.0, 0.1, 0.0],
            [0.1, 0.1, 0.0],
            [0.2, 0.1, 0.0],
        ]
    ).reshape(-1, 1, 3)
    pixels, _ = cv2.fisheye.projectPoints(
        objects,
        np.array([[0.25], [-0.2], [0.1]]),
        np.array([[0.02], [-0.03], [0.8]]),
        camera,
        distortion,
    )

    accurate = _fisheye_heldout_rms([objects], [pixels], camera, distortion)
    perturbed = pixels.copy()
    perturbed[0, 0] += [8.0, -7.0]
    corrupted = _fisheye_heldout_rms([objects], [perturbed], camera, distortion)

    assert accurate < 1e-4
    assert corrupted > 0.5


def test_exported_apriltag_pinhole_calibration_loads_and_detects(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    rotations = [
        np.array([0.35 * np.sin(index), 0.35 * np.cos(index), 0.1 * index]) for index in range(25)
    ]
    _evidence(evidence, rotations)
    frames = tmp_path / "frames"
    frames.mkdir()
    for index in range(25):
        source = np.full((720, 1280, 3), 255, np.uint8)
        source[0, 0, 0] = index
        assert cv2.imwrite(str(frames / f"frame-{index * 10:06}.png"), source)
    artifact = export_tag_calibration(
        TagCandidateRequest(
            evidence=evidence, tag_size_m=0.199898, pipeline=_pipeline(), frames_dir=frames
        ),
        camera_serial="fixture-camera",
        evidence_kind="synthetic",
        allow_synthetic=True,
    )
    detector = CameraTagDetector(
        artifact, camera_serial="fixture-camera", tag_sizes_m={7: 0.199898}, allow_synthetic=True
    )
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 7, 300
    )
    image = np.full((720, 1280), 255, np.uint8)
    image[210:510, 490:790] = marker

    observations = detector.detect(image)

    assert observations[0]["tag_id"] == 7


def test_recorded_live_export_revalidates_rendered_raw_capture_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    camera = np.array([[850.0, 0.0, 640.0], [0.0, 830.0, 360.0], [0.0, 0.0, 1.0]])
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 14, 1200
    )
    marker_corners = np.float32([[0, 0], [1199, 0], [1199, 1199], [0, 1199]])
    marker_points = np.array(
        [[0, 0, 0], [0.199898, 0, 0], [0.199898, 0.199898, 0], [0, 0.199898, 0]],
        np.float32,
    )
    raws = []
    for index in range(26):
        pixels, _ = cv2.projectPoints(
            marker_points,
            np.array([0.35 * np.sin(index * 0.7), 0.35 * np.cos(index * 0.45), 0.1 * index]),
            np.array([(index % 5 - 2) * 0.08, (index % 4 - 1.5) * 0.06, 1.4 + index * 0.02]),
            camera,
            None,
        )
        image = np.full((720, 1280), 255, np.uint8)
        image = cv2.warpPerspective(
            marker,
            cv2.getPerspectiveTransform(marker_corners, pixels.reshape(4, 2).astype(np.float32)),
            (1280, 720),
            dst=image,
            borderMode=cv2.BORDER_TRANSPARENT,
        )
        raws.append(
            cv2.cvtColor(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2YUV_UYVY).tobytes()
        )

    def check_output(command, **_kwargs):
        if "boot_id" in command[-1]:
            return "boot-1\n"
        _, _, _, name, pixel_format, shape, vendor, product = dual_capture.CAMERAS[0]
        return (
            "usb_parent=/sys/devices/usb/video0\n"
            f"id_vendor={vendor}\n"
            f"id_product={product}\n"
            f"name={name}\n"
            "Format Video Capture:\n"
            f"\tWidth/Height : {shape[0]}/{shape[1]}\n"
            f"\tPixel Format : '{pixel_format}'\n"
        )

    def run(command, **_kwargs):
        if "pull" in command:
            path = Path(command[-1])
            index = int(re.search(r"raw-(\d+)", path.name)[1])
            path.write_bytes(raws[index])
        return subprocess.CompletedProcess(command, 0)

    def fingerprint(_serial, remote, _deadline_ns):
        index = int(re.search(r"-(\d+)-main", remote)[1])
        return len(raws[index]), hashlib.sha256(raws[index]).hexdigest()

    monkeypatch.setattr(dual_capture.subprocess, "check_output", check_output)
    monkeypatch.setattr(dual_capture.subprocess, "run", run)
    monkeypatch.setattr(dual_capture, "_remote_raw_fingerprint", fingerprint)
    monkeypatch.setattr(dual_capture.time, "monotonic_ns", lambda: 100)
    capture = tmp_path / "capture"
    dual_capture.run(
        "serial-1",
        capture,
        expected_boot_id="boot-1",
        count=len(raws),
        duration_s=30,
        camera="main",
    )

    manifest = json.loads((capture / "manifest.json").read_text())
    pipeline = manifest["cameras"]["main"]["calibration_pipeline"] | {
        "capture_pipeline_sha256": manifest["capture_pipeline_sha256"],
        "fov_bounds_deg": {"horizontal": [70, 80], "vertical": [40, 55]},
    }
    request = TagCandidateRequest(
        evidence=capture / "main" / "result.json",
        tag_size_m=0.199898,
        pipeline=pipeline,
        frames_dir=capture / "main",
        minimum_frame_gap=1,
        maximum_views=len(raws),
    )

    artifact = export_tag_calibration(
        request, camera_serial="fixture-camera", evidence_kind="recorded_live"
    )

    assert artifact["accepted_image_count"] == len(raws)

    raw = capture / "main" / "raw-000000.uyvy"
    raw_bytes = raw.read_bytes()
    raw.write_bytes(b"\0" + raw_bytes[1:])
    with pytest.raises(ValueError, match="raw source changed"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    raw.write_bytes(raw_bytes)

    png = capture / "main" / "frame-000000.png"
    png_bytes = png.read_bytes()
    png.write_bytes(b"\0" + png_bytes[1:])
    with pytest.raises(ValueError, match="source image|raster"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    png.write_bytes(png_bytes)

    evidence_bytes = request.evidence.read_bytes()
    frame_record = capture / "main" / "frame-000000.json"
    frame_record_bytes = frame_record.read_bytes()
    changed_raw = bytearray(raw_bytes)
    changed_raw[0] = 0 if changed_raw[0] else 255
    raw.write_bytes(changed_raw)
    changed_hash = hashlib.sha256(changed_raw).hexdigest()
    for document_path, document_bytes in (
        (request.evidence, evidence_bytes),
        (frame_record, frame_record_bytes),
    ):
        document = json.loads(document_bytes)
        frame = document["frames"][0] if document_path == request.evidence else document
        frame["source_sha256"] = changed_hash
        frame["source_device_sha256"] = changed_hash
        document_path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="does not decode"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    raw.write_bytes(raw_bytes)
    request.evidence.write_bytes(evidence_bytes)
    frame_record.write_bytes(frame_record_bytes)

    evidence = json.loads(evidence_bytes)
    evidence["frames"][0]["corners_px"][0][0][0] += 0.1
    request.evidence.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="corners"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    request.evidence.write_bytes(evidence_bytes)

    bad_pipeline = pipeline | {"camera_mode": "/dev/video0 1x1"}
    with pytest.raises(ValueError, match="pipeline"):
        export_tag_calibration(
            TagCandidateRequest(
                evidence=request.evidence,
                tag_size_m=request.tag_size_m,
                pipeline=bad_pipeline,
                frames_dir=request.frames_dir,
                minimum_frame_gap=request.minimum_frame_gap,
                maximum_views=request.maximum_views,
            ),
            camera_serial="fixture-camera",
            evidence_kind="recorded_live",
        )

    bad_device = pipeline | {"android_device_id": "different-serial"}
    with pytest.raises(ValueError, match="pipeline"):
        export_tag_calibration(
            TagCandidateRequest(
                evidence=request.evidence,
                tag_size_m=request.tag_size_m,
                pipeline=bad_device,
                frames_dir=request.frames_dir,
                minimum_frame_gap=request.minimum_frame_gap,
                maximum_views=request.maximum_views,
            ),
            camera_serial="fixture-camera",
            evidence_kind="recorded_live",
        )

    evidence = json.loads(evidence_bytes)
    evidence["frames"][0]["boot_id"] = "different-boot"
    request.evidence.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="capture boot"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    request.evidence.write_bytes(evidence_bytes)

    snapshot = capture / "main" / "manifest.json"
    snapshot_bytes = snapshot.read_bytes()
    manifest = json.loads(snapshot_bytes)
    manifest["capture_pipeline"]["main"]["usb_parent"] = "/different"
    snapshot.write_text(json.dumps(manifest))
    evidence = json.loads(evidence_bytes)
    evidence["recorded_live_provenance"]["manifest_sha256"] = hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()
    request.evidence.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="pipeline binding"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    snapshot.write_bytes(snapshot_bytes)
    request.evidence.write_bytes(evidence_bytes)

    evidence = json.loads(evidence_bytes)
    evidence.pop("recorded_live_provenance")
    request.evidence.write_text(json.dumps(evidence))
    with pytest.raises(ValueError, match="capture provenance"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )


def test_recorded_live_session_exports_multiple_verified_capture_collections(
    tmp_path: Path, monkeypatch
) -> None:
    camera = np.array([[850.0, 0.0, 640.0], [0.0, 830.0, 360.0], [0.0, 0.0, 1.0]])
    marker = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), 14, 1200
    )
    marker_corners = np.float32([[0, 0], [1199, 0], [1199, 1199], [0, 1199]])
    marker_points = np.array(
        [[0, 0, 0], [0.199898, 0, 0], [0.199898, 0.199898, 0], [0, 0.199898, 0]],
        np.float32,
    )
    raws = []
    for index in range(26):
        pixels, _ = cv2.projectPoints(
            marker_points,
            np.array([0.35 * np.sin(index * 0.7), 0.35 * np.cos(index * 0.45), 0.1 * index]),
            np.array([(index % 5 - 2) * 0.08, (index % 4 - 1.5) * 0.06, 1.4 + index * 0.02]),
            camera,
            None,
        )
        image = np.full((720, 1280), 255, np.uint8)
        image = cv2.warpPerspective(
            marker,
            cv2.getPerspectiveTransform(marker_corners, pixels.reshape(4, 2).astype(np.float32)),
            (1280, 720),
            dst=image,
            borderMode=cv2.BORDER_TRANSPARENT,
        )
        raws.append(
            cv2.cvtColor(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2YUV_UYVY).tobytes()
        )

    boot = "boot-1"
    active_raws: list[bytes] = []

    def check_output(command, **_kwargs):
        if "boot_id" in command[-1]:
            return f"{boot}\n"
        _, _, _, name, pixel_format, shape, vendor, product = dual_capture.CAMERAS[0]
        return (
            "usb_parent=/sys/devices/usb/video0\n"
            f"id_vendor={vendor}\n"
            f"id_product={product}\n"
            f"name={name}\n"
            "Format Video Capture:\n"
            f"\tWidth/Height : {shape[0]}/{shape[1]}\n"
            f"\tPixel Format : '{pixel_format}'\n"
        )

    def run(command, **_kwargs):
        if "pull" in command:
            index = int(re.search(r"raw-(\d+)", Path(command[-1]).name)[1])
            Path(command[-1]).write_bytes(active_raws[index])
        return subprocess.CompletedProcess(command, 0)

    def fingerprint(_serial, remote, _deadline_ns):
        index = int(re.search(r"-(\d+)-main", remote)[1])
        raw = active_raws[index]
        return len(raw), hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(dual_capture.subprocess, "check_output", check_output)
    monkeypatch.setattr(dual_capture.subprocess, "run", run)
    monkeypatch.setattr(dual_capture, "_remote_raw_fingerprint", fingerprint)
    monkeypatch.setattr(dual_capture.time, "monotonic_ns", lambda: 100)
    first, second = tmp_path / "capture-a", tmp_path / "capture-b"
    active_raws = raws[:13]
    dual_capture.run(
        "serial-1",
        first,
        expected_boot_id=boot,
        count=len(active_raws),
        duration_s=30,
        camera="main",
    )
    boot = "boot-2"
    active_raws = raws[13:]
    dual_capture.run(
        "serial-1",
        second,
        expected_boot_id=boot,
        count=len(active_raws),
        duration_s=30,
        camera="main",
    )

    session = tmp_path / "session"
    create_session(session, [first / "main", second / "main"])
    source_manifest = json.loads((first / "manifest.json").read_text())
    pipeline = source_manifest["cameras"]["main"]["calibration_pipeline"] | {
        "capture_pipeline_sha256": source_manifest["capture_pipeline_sha256"],
        "fov_bounds_deg": {"horizontal": [70, 80], "vertical": [40, 55]},
    }
    request = TagCandidateRequest(
        evidence=session / "result.json",
        tag_size_m=0.199898,
        pipeline=pipeline,
        frames_dir=session,
        minimum_frame_gap=1,
        maximum_views=26,
    )

    artifact = export_tag_calibration(
        request, camera_serial="fixture-camera", evidence_kind="recorded_live"
    )

    assert artifact["accepted_image_count"] == 26

    result_path, session_path = session / "result.json", session / "session.json"
    result_bytes, session_bytes = result_path.read_bytes(), session_path.read_bytes()

    result = json.loads(result_bytes)
    result["frames"][0]["corners_px"][0][0][0] += 0.1
    result_path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match="aggregate frame"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    result_path.write_bytes(result_bytes)

    def replace_session(mutator) -> None:
        session_document = json.loads(session_bytes)
        mutator(session_document)
        session_path.write_text(json.dumps(session_document, sort_keys=True) + "\n")
        result = json.loads(result_bytes)
        result["recorded_live_provenance"]["session_sha256"] = hashlib.sha256(
            session_path.read_bytes()
        ).hexdigest()
        result_path.write_text(json.dumps(result, sort_keys=True) + "\n")

    replace_session(
        lambda value: value["sources"][0]["files"].__setitem__(
            "sources/source-000000/raw-000000.uyvy", "0" * 64
        )
    )
    with pytest.raises(ValueError, match="source file changed"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    session_path.write_bytes(session_bytes)
    result_path.write_bytes(result_bytes)

    replace_session(lambda value: value["frames"][0].__setitem__("source_session_index", 1))
    with pytest.raises(ValueError, match="frame mapping is duplicated"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    session_path.write_bytes(session_bytes)
    result_path.write_bytes(result_bytes)

    replace_session(
        lambda value: value["sources"][1]["camera_pipeline"].__setitem__(
            "android_device_id", "different-device"
        )
    )
    with pytest.raises(ValueError, match="camera pipelines differ"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )


def test_unknown_fov_fisheye_candidate_refuses_one_capture_collection(tmp_path: Path) -> None:
    evidence = tmp_path / "corners.json"
    rotations = [
        np.array([0.18 * np.sin(index), 0.18 * np.cos(index), 0.04 * index]) for index in range(25)
    ]
    _evidence(evidence, rotations)
    document = json.loads(evidence.read_text())
    frames = tmp_path / "frames"
    frames.mkdir()
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    marker = cv2.aruco.generateImageMarker(dictionary, 14, 800)
    source = np.float32([[0, 0], [800, 0], [800, 800], [0, 800]])
    for frame in document["frames"]:
        corners = np.asarray(frame["corners_px"][0], dtype=np.float32)
        frame["tag_ids"] = [14]
        frame["corners_px"] = [corners.tolist()]
        image = cv2.warpPerspective(
            marker,
            cv2.getPerspectiveTransform(source, corners),
            (1280, 720),
            borderValue=255,
        )
        assert cv2.imwrite(str(frames / f"frame-{frame['frame_index']:06}.png"), image)
    evidence.write_text(json.dumps(document))
    pipeline = _pipeline()
    pipeline.pop("fov_bounds_deg")

    result = calibrate_tag_candidate(
        TagCandidateRequest(
            evidence=evidence,
            tag_size_m=0.199898,
            pipeline=pipeline,
            frames_dir=frames,
            model="fisheye",
            minimum_frame_gap=1,
            maximum_views=25,
        )
    )

    assert result["status"] == "rejected"
    assert "unknown-FOV fisheye qualification failed" in result["rejection_reasons"]


def test_unknown_fov_fisheye_candidate_accepts_verified_diverse_rasters(
    tmp_path: Path, monkeypatch
) -> None:
    width, height, scale, tag_size_m = 1280, 720, 3, 0.125
    camera = np.array([[600.0, 0.0, 640.0], [0.0, 595.0, 360.0], [0.0, 0.0, 1.0]])
    distortion = np.array([[-0.08], [0.01], [-0.001], [0.0001]])
    scaled_camera = camera.copy()
    scaled_camera[:2] *= scale
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    marker = cv2.aruco.generateImageMarker(dictionary, 56, 4800)
    base_rotation = np.diag([1.0, -1.0, -1.0])
    positions = [
        (0.0, 0.0),
        (0.22, 0.0),
        (-0.22, 0.0),
        (0.0, 0.22),
        (0.0, -0.22),
        (0.34, 0.20),
        (-0.34, 0.20),
        (0.34, -0.20),
        (-0.34, -0.20),
        (0.0, 0.38),
        (0.0, -0.38),
        (0.40, 0.0),
    ]

    def render(rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
        projected, _ = cv2.fisheye.projectPoints(
            tag_corners(tag_size_m).reshape(-1, 1, 3),
            cv2.Rodrigues(rotation)[0],
            translation.reshape(3, 1),
            camera,
            distortion,
        )
        left, top = np.floor(projected.reshape(-1, 2).min(axis=0) * scale - 8).astype(int)
        right, bottom = np.ceil(projected.reshape(-1, 2).max(axis=0) * scale + 8).astype(int)
        left, top = max(0, left), max(0, top)
        right, bottom = min(width * scale, right), min(height * scale, bottom)
        raster = np.full((height * scale, width * scale), 255, np.uint8)
        x, y = np.meshgrid(np.arange(left, right, dtype=float), np.arange(top, bottom, dtype=float))
        normalized = cv2.fisheye.undistortPoints(
            np.dstack((x, y)).reshape(-1, 1, 2), scaled_camera, distortion
        ).reshape(bottom - top, right - left, 2)
        rays = np.dstack((normalized, np.ones((bottom - top, right - left))))
        depth = float(np.dot(rotation[:, 2], translation)) / (rays @ rotation[:, 2])
        tag = (rays * depth[:, :, None] - translation) @ rotation
        marker_x = ((tag[:, :, 0] / tag_size_m) + 0.5) * (marker.shape[1] - 1)
        marker_y = (0.5 - (tag[:, :, 1] / tag_size_m)) * (marker.shape[0] - 1)
        raster[top:bottom, left:right] = cv2.remap(
            marker,
            marker_x.astype(np.float32),
            marker_y.astype(np.float32),
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
        return cv2.resize(raster, (width, height), interpolation=cv2.INTER_AREA)

    raws: list[bytes] = []
    for collection in range(8):
        for view, (x, y) in enumerate(positions):
            rotation_vector = np.array(
                [
                    -0.20 + 0.030 * (view % 7) + 0.00001 * (collection - 3.5),
                    -0.18 + 0.030 * ((2 * view) % 7) + 0.00001 * (collection - 3.5),
                    -0.14 + 0.025 * ((3 * view) % 7) + 0.000008 * (collection - 3.5),
                ]
            )
            rotation = cv2.Rodrigues(rotation_vector)[0] @ base_rotation
            depth = 0.64 + 0.015 * (view % 6) + 0.00001 * (collection - 3.5)
            image = render(rotation, np.array([x * depth, y * depth, depth]))
            raws.append(
                cv2.cvtColor(
                    cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2YUV_UYVY
                ).tobytes()
            )
    assert len(raws) == 96

    active_raws: list[bytes] = []
    boot = "fixture-boot"

    def check_output(command, **_kwargs):
        if "boot_id" in command[-1]:
            return f"{boot}\n"
        _, _, _, name, pixel_format, shape, vendor, product = dual_capture.CAMERAS[0]
        return (
            "usb_parent=/sys/devices/usb/video0\n"
            f"id_vendor={vendor}\n"
            f"id_product={product}\n"
            f"name={name}\n"
            "Format Video Capture:\n"
            f"\tWidth/Height : {shape[0]}/{shape[1]}\n"
            f"\tPixel Format : '{pixel_format}'\n"
        )

    def run(command, **_kwargs):
        if "pull" in command:
            index = int(re.search(r"raw-(\d+)", Path(command[-1]).name)[1])
            Path(command[-1]).write_bytes(active_raws[index])
        return subprocess.CompletedProcess(command, 0)

    def fingerprint(_serial, remote, _deadline_ns):
        index = int(re.search(r"-(\d+)-main", remote)[1])
        raw = active_raws[index]
        return len(raw), hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(dual_capture.subprocess, "check_output", check_output)
    monkeypatch.setattr(dual_capture.subprocess, "run", run)
    monkeypatch.setattr(dual_capture, "_remote_raw_fingerprint", fingerprint)
    monkeypatch.setattr(dual_capture.time, "monotonic_ns", lambda: 100)
    sources = []
    for collection in range(8):
        active_raws = [
            raws[collection * len(positions) + ((view + 4 * collection) % len(positions))]
            for view in range(len(positions))
        ]
        capture = tmp_path / f"capture-{collection}"
        dual_capture.run(
            "fixture-serial",
            capture,
            expected_boot_id=boot,
            count=len(active_raws),
            duration_s=30,
            camera="main",
        )
        sources.append(capture / "main")

    source_session = tmp_path / "source-session"
    create_session(source_session, sources)
    session = tmp_path / "aggregate-session"
    shutil.copytree(source_session, session)
    session_path = session / "session.json"
    session_document = json.loads(session_path.read_text())
    position_groups = []
    for collection in range(8):
        evidence_file = f"synthetic-position-{collection}.json"
        payload = json.dumps({"kind": "synthetic_fixture", "position": collection}, sort_keys=True)
        (session / evidence_file).write_text(payload)
        source = session_document["sources"][collection]
        position_groups.append(
            {
                "position_group_id": f"synthetic-position-{collection}",
                "evidence_kind": "synthetic",
                "base_pose_evidence_file": evidence_file,
                "base_pose_evidence_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                "sources": [
                    {
                        "source_session_index": collection,
                        "raw_capture_collection": source["raw_capture_collection"],
                        "manifest_sha256": source["manifest_sha256"],
                    }
                ],
            }
        )
    session_document["physical_position_groups"] = position_groups
    result_path = session / "result.json"
    session_result = json.loads(result_path.read_text())

    def write_aggregate_evidence() -> None:
        session_path.write_text(json.dumps(session_document, sort_keys=True) + "\n")
        session_result["recorded_live_provenance"]["session_sha256"] = hashlib.sha256(
            session_path.read_bytes()
        ).hexdigest()
        result_path.write_text(json.dumps(session_result, sort_keys=True) + "\n")

    write_aggregate_evidence()
    manifest = json.loads((sources[0].parent / "manifest.json").read_text())
    pipeline = manifest["cameras"]["main"]["calibration_pipeline"] | {
        "capture_pipeline_sha256": manifest["capture_pipeline_sha256"]
    }
    request = TagCandidateRequest(
        evidence=session / "result.json",
        tag_size_m=tag_size_m,
        pipeline=pipeline,
        frames_dir=session,
        minimum_frame_gap=1,
        maximum_views=96,
        model="fisheye",
    )

    result = calibrate_tag_candidate(request)

    assert result["status"] == "candidate"
    assert result["selected_observation_count"] == 96
    assert result["camera_matrix"][0][0] == pytest.approx(camera[0, 0], rel=0.01)
    assert result["camera_matrix"][1][1] == pytest.approx(camera[1, 1], rel=0.01)
    qualification = result["quality"]["unknown_fov_qualification"]
    assert qualification["passes"] is True
    assert (
        qualification["empirical_leave_declared_physical_position_out_stability"]["fold_count"] == 8
    )
    fitted_camera = np.asarray(result["camera_matrix"], dtype=float)
    fitted_distortion = np.asarray(result["distortion_coefficients"], dtype=float)
    pixels = np.asarray([[400, 240], [520, 260], [640, 360], [760, 460], [880, 480]], dtype=float)
    rays = cv2.fisheye.undistortPoints(pixels.reshape(-1, 1, 2), camera, distortion)
    remapped = cv2.fisheye.distortPoints(rays, fitted_camera, fitted_distortion).reshape(-1, 2)
    assert np.max(np.linalg.norm(remapped - pixels, axis=1)) < 3

    with pytest.raises(ValueError, match="reviewed real position groups"):
        export_tag_calibration(
            request, camera_serial="fixture-camera", evidence_kind="recorded_live"
        )
    artifact = export_tag_calibration(
        request,
        camera_serial="fixture-camera",
        evidence_kind="synthetic",
        allow_synthetic=True,
    )
    assert artifact["accepted_image_count"] == 96
    descriptor = artifact["tag_candidate_quality"]["unknown_fov_qualification"][
        "physical_position_groups"
    ]
    assert descriptor["parser_verification"] == {
        "session_sha256": True,
        "base_pose_evidence_sha256": True,
        "selected_source_bindings": True,
        "physical_position_independence": "operator_attested",
    }
    assert descriptor["session_file"] == "session.json"
    assert (
        descriptor["session_sha256"] == session_result["recorded_live_provenance"]["session_sha256"]
    )
    assert descriptor["selected_position_group_ids"] == [
        f"synthetic-position-{collection}" for collection in range(8)
    ]
    assert descriptor["groups"][0] == {
        "position_group_id": "synthetic-position-0",
        "evidence_kind": "synthetic",
        "base_pose_evidence_file": "synthetic-position-0.json",
        "base_pose_evidence_sha256": position_groups[0]["base_pose_evidence_sha256"],
        "sources": [position_groups[0]["sources"][0]],
    }

    for group in position_groups:
        group["evidence_kind"] = "reviewed_real"
    write_aggregate_evidence()
    bare_review = calibrate_tag_candidate(request)
    assert bare_review["status"] == "rejected"
    assert "unknown-FOV fisheye qualification failed" in bare_review["rejection_reasons"]

    for group in position_groups:
        group["review"] = {
            "reviewer": "fixture-reviewer",
            "decision": "independent_physical_position",
        }
    write_aggregate_evidence()
    live_artifact = export_tag_calibration(
        request, camera_serial="fixture-camera", evidence_kind="recorded_live"
    )
    live_descriptor = live_artifact["tag_candidate_quality"]["unknown_fov_qualification"][
        "physical_position_groups"
    ]
    assert live_descriptor["review_assertion_scope"] == "trusted_operator_attestation"
    assert live_descriptor["groups"][0]["review"] == position_groups[0]["review"]

    del position_groups[0]["sources"][0]["manifest_sha256"]
    write_aggregate_evidence()
    missing_binding = calibrate_tag_candidate(request)
    assert missing_binding["status"] == "rejected"
    assert "unknown-FOV fisheye qualification failed" in missing_binding["rejection_reasons"]
