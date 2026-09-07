"""Camera-frame AprilTag observations; no map or flight authorization is produced."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import cv2
import numpy as np

from calibration.fisheye import rectification_maps
from perception.tag_localization import tag_corners
from tools.map_common import finite_number, parse_document

MAX_CALIBRATION_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_TAGS_PER_FRAME = 64


def read_calibration(path: Path, expected_sha256: str) -> dict[str, object]:
    """Read a bounded regular-file snapshot and verify its pinned hash."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CALIBRATION_BYTES:
            raise ValueError("calibration must be a regular file of at most 1 MiB")
        payload = source.read(MAX_CALIBRATION_BYTES + 1)
    if len(payload) > MAX_CALIBRATION_BYTES:
        raise ValueError("calibration exceeds 1 MiB")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("calibration hash mismatch")
    return parse_document(payload, "camera calibration")


class CameraTagDetector:
    """Camera axes are right/down/forward; printed tag axes are right/up/outward."""

    def __init__(
        self,
        calibration: dict[str, object],
        *,
        camera_serial: str,
        tag_sizes_m: dict[int, float],
        allow_synthetic: bool = False,
    ):
        version = calibration.get("schema_version")
        if type(version) is not int or version not in (1, 2):
            raise ValueError("unsupported calibration schema")
        self.model = "pinhole" if version == 1 else calibration.get("model")
        if (
            self.model not in ("pinhole", "fisheye")
            or (version == 1 and calibration.get("model", "pinhole") != "pinhole")
            or (version == 2 and self.model != "fisheye")
        ):
            raise ValueError("unsupported calibration model")
        if (
            not isinstance(camera_serial, str)
            or not camera_serial
            or calibration.get("camera_serial") != camera_serial
            or calibration.get("status") != "offline"
        ):
            raise ValueError("camera identity or calibration status mismatch")
        self.camera_serial = camera_serial
        self.evidence_kind = calibration.get("evidence_kind")
        if self.evidence_kind not in ("recorded_live", "synthetic") or (
            self.evidence_kind == "synthetic" and not allow_synthetic
        ):
            raise ValueError("recorded calibration is required; synthetic use must be explicit")
        size = calibration.get("image_size_px")
        pipeline = calibration.get("pipeline")
        if (
            not isinstance(size, list)
            or len(size) != 2
            or any(type(value) is not int or value <= 0 for value in size)
            or size[0] * size[1] > MAX_IMAGE_PIXELS
            or not isinstance(pipeline, dict)
            or pipeline.get("resolution_px") != size
        ):
            raise ValueError("invalid or mismatched calibration resolution")
        self.width, self.height = size
        self.pipeline = pipeline.copy()
        count = calibration.get("accepted_image_count")
        hashes = calibration.get("image_sha256")
        rms = finite_number(calibration.get("rms_reprojection_error_px"), "calibration RMS")
        if (
            type(count) is not int
            or not 20 <= count <= 10000
            or not 0 <= rms < 0.5
            or not isinstance(hashes, dict)
            or len(hashes) != count
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in hashes.values()
            )
            or len(set(hashes.values())) != count
        ):
            raise ValueError("invalid calibration quality evidence")
        if self.model == "fisheye":
            quality = calibration.get("quality")
            if (
                not isinstance(quality, dict)
                or quality.get("accepted_image_count") != count
                or quality.get("minimum_accepted_image_count") != 20
                or quality.get("rms_reprojection_error_px") != rms
                or quality.get("maximum_rms_reprojection_error_px") != 0.5
                or quality.get("minimum_pose_constraint_ratio") != 0.005
                or quality.get("opencv_check_cond") is not True
                or not 0.005 <= finite_number(quality.get("pose_constraint_ratio")) <= 1
            ):
                raise ValueError("invalid fisheye conditioning evidence")
        self.K = np.asarray(calibration.get("camera_matrix"), dtype=float)
        self.D = np.asarray(calibration.get("distortion_coefficients"), dtype=float)
        lengths = (4,) if self.model == "fisheye" else (4, 5, 8, 12, 14)
        if (
            self.K.shape != (3, 3)
            or not np.isfinite(self.K).all()
            or self.K[0, 0] <= 0
            or self.K[1, 1] <= 0
            or self.K[0, 1] != 0
            or self.K[1, 0] != 0
            or not np.array_equal(self.K[2], [0, 0, 1])
            or not 0 <= self.K[0, 2] < self.width
            or not 0 <= self.K[1, 2] < self.height
            or self.D.ndim != 1
            or self.D.size not in lengths
            or not np.isfinite(self.D).all()
        ):
            raise ValueError("invalid camera intrinsics")
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        if not isinstance(tag_sizes_m, dict) or not 1 <= len(tag_sizes_m) <= MAX_TAGS_PER_FRAME:
            raise ValueError("declare between one and 64 tag sizes")
        self.tag_sizes = {}
        for identifier, size_m in tag_sizes_m.items():
            size_m = finite_number(size_m, "tag size")
            if (
                type(identifier) is not int
                or not 0 <= identifier < dictionary.bytesList.shape[0]
                or not 0 < size_m <= 10
            ):
                raise ValueError("invalid tag36h11 identifier or metric tag size")
            self.tag_sizes[identifier] = size_m
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(dictionary, parameters)
        self._remap = None
        if self.model == "fisheye":
            self._remap = rectification_maps(self.K, self.D, (self.width, self.height))

    def detect(self, image: np.ndarray) -> list[dict[str, object]]:
        """Return decoded IDs even when a unique camera-frame pose cannot be established."""
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.uint8
            or image.shape not in ((self.height, self.width), (self.height, self.width, 3))
        ):
            raise ValueError("frame must match the calibrated Gray8 or BGR8 resolution")
        if self._remap is not None:
            image = cv2.remap(image, *self._remap, cv2.INTER_LINEAR)
        corners, ids, _ = self.detector.detectMarkers(image)
        if ids is None:
            return []
        if len(ids) > MAX_TAGS_PER_FRAME:
            raise ValueError("frame exceeds the 64-tag observation limit")
        identifiers = ids.flatten().tolist()
        observations = []
        for corner, identifier in zip(corners, identifiers, strict=True):
            pixels = corner.reshape(4, 2).astype(float)
            observation = {
                "tag_id": identifier,
                "family": "tag36h11",
                "corners_px": pixels.tolist(),
                "pixel_frame": "rectified_camera" if self._remap is not None else "camera",
                "size_m": self.tag_sizes.get(identifier),
                "pose_accepted": False,
                "T_camera_tag": None,
                "reprojection_rms_px": None,
                "verified_for_flight": False,
            }
            if identifiers.count(identifier) != 1:
                observation["reason"] = "duplicate_tag"
            elif identifier not in self.tag_sizes:
                observation["reason"] = "unconfigured_tag"
            elif np.min(np.linalg.norm(pixels - np.roll(pixels, 1, axis=0), axis=1)) < 20:
                observation["reason"] = "tag_too_small"
            else:
                observation.update(self._pose(pixels, self.tag_sizes[identifier]))
            observations.append(observation)
        return observations

    def _pose(self, pixels: np.ndarray, size_m: float) -> dict[str, object]:
        points = tag_corners(size_m).astype(float)
        distortion = np.zeros(4) if self._remap is not None else self.D
        result = cv2.solvePnPGeneric(
            points, pixels, self.K, distortion, flags=cv2.SOLVEPNP_IPPE_SQUARE
        )
        candidates = []
        for rotation_vector, translation in zip(result[1], result[2], strict=True):
            rotation = cv2.Rodrigues(rotation_vector)[0]
            translation = translation.reshape(3)
            if not np.isfinite(rotation).all() or not np.isfinite(translation).all():
                continue
            camera_points = points @ rotation.T + translation
            # The printed tag's +Z normal must face its observing camera.
            if camera_points[:, 2].min() <= 0 or np.dot(rotation[:, 2], -translation) <= 0:
                continue
            projected = cv2.projectPoints(points, rotation_vector, translation, self.K, distortion)[
                0
            ].reshape(4, 2)
            error = float(np.sqrt(np.mean(np.sum((projected - pixels) ** 2, axis=1))))
            if not np.isfinite(error):
                continue
            transform = np.eye(4)
            transform[:3, :3], transform[:3, 3] = rotation, translation
            candidates.append((error, transform))
        candidates.sort(key=lambda item: item[0])
        if not candidates or candidates[0][0] > 2:
            return {"reason": "reprojection_or_cheirality"}
        best_error, transform = candidates[0]
        if len(candidates) > 1 and (
            candidates[1][0] - best_error < 0.5 or candidates[1][0] < 2 * max(best_error, 1e-9)
        ):
            return {"reason": "ambiguous", "reprojection_rms_px": best_error}
        return {
            "reason": "pose",
            "pose_accepted": True,
            "T_camera_tag": transform.tolist(),
            "reprojection_rms_px": best_error,
        }
