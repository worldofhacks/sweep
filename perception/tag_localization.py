"""Offline tag localization. Matrices map column vectors between named frames."""

import hashlib
from pathlib import Path

import cv2
import numpy as np

from tools.map_common import parse_document, validate_transform
from tools.map_validate import validate_bundle


def rigid(value):
    return np.array(validate_transform(np.asarray(value).tolist()), dtype=float)


def tag_corners(size):
    """Canonical decoded TL, TR, BR, BL in the printed tag's right/up/out frame."""
    return np.array([[-1, 1, 0], [1, 1, 0], [1, -1, 0], [-1, -1, 0]]) * size / 2


def map_points(tag):
    transform = rigid(tag["T_map_tag"])
    return tag_corners(tag["size"]) @ transform[:3, :3].T + transform[:3, 3]


class TagLocalizer:
    def __init__(
        self,
        bundle,
        accepted_versions,
        calibration_path,
        calibration_sha256,
        camera_serial,
        pipeline,
        T_body_camera,
    ):
        self.manifest = validate_bundle(bundle, accepted_versions)
        calibration_path = Path(calibration_path)
        payload = calibration_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != calibration_sha256:
            raise ValueError("calibration hash mismatch")
        calibration = parse_document(payload, str(calibration_path))
        if (
            type(calibration.get("schema_version")) is not int
            or calibration["schema_version"] != 1
            or calibration.get("camera_serial") != camera_serial
            or calibration.get("pipeline") != pipeline
            or calibration.get("image_size_px") != [1280, 720]
            or pipeline.get("resolution_px") != [1280, 720]
        ):
            raise ValueError("camera configuration mismatch")
        self.evidence_kind = calibration.get("evidence_kind")
        if calibration.get("status") != "offline" or self.evidence_kind not in (
            "synthetic",
            "recorded_live",
        ):
            raise ValueError("invalid calibration evidence")
        count = calibration.get("accepted_image_count")
        rms = calibration.get("rms_reprojection_error_px")
        hashes = calibration.get("image_sha256")
        if (
            type(count) is not int
            or count < 20
            or type(rms) not in (int, float)
            or not np.isfinite(rms)
            or not 0 <= rms < 0.5
            or not isinstance(hashes, dict)
            or len(hashes) != count
            or any(
                not isinstance(h, str)
                or len(h) != 64
                or any(c not in "0123456789abcdef" for c in h)
                for h in hashes.values()
            )
            or len(set(hashes.values())) != count
        ):
            raise ValueError("invalid calibration quality evidence")
        self.K = np.array(calibration["camera_matrix"], dtype=float)
        self.dist = np.array(calibration["distortion_coefficients"], dtype=float)
        if (
            self.K.shape != (3, 3)
            or not np.isfinite(self.K).all()
            or self.K[0, 1] != 0
            or self.K[1, 0] != 0
            or not 0 <= self.K[0, 2] < 1280
            or not 0 <= self.K[1, 2] < 720
            or self.K[0, 0] <= 0
            or self.K[1, 1] <= 0
            or not np.allclose(self.K[2], [0, 0, 1])
            or self.dist.ndim != 1
            or self.dist.size not in (4, 5, 8, 12, 14)
            or not np.isfinite(self.dist).all()
        ):
            raise ValueError("invalid camera intrinsics")
        self.T_body_camera = rigid(T_body_camera)
        self.tags = {t["id"]: t for t in self.manifest.document("tags.yaml")["tags"]}
        self.calibration_sha256 = calibration_sha256
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(dictionary, parameters)

    def estimate(self, image, capture_time, decode_time, now, max_age=0.5):
        """Times are seconds in one monotonic clock; capture time must be measured upstream."""
        times = np.array([capture_time, decode_time, now, max_age], dtype=float)
        if (
            not np.isfinite(times).all()
            or capture_time < 0
            or not capture_time <= decode_time <= now
            or max_age <= 0
        ):
            raise ValueError("invalid frame timing")
        report = dict(
            accepted=False,
            flight_approved=False,
            capture_time=capture_time,
            decode_time=decode_time,
            age_s=now - capture_time,
            map_sha256=self.manifest["content_sha256"],
            calibration_sha256=self.calibration_sha256,
            T_body_camera=self.T_body_camera.tolist(),
            timing_provenance="upstream_capture_clock",
            calibration_evidence_kind=self.evidence_kind,
        )
        if now - capture_time > max_age:
            return report | {"reason": "stale"}
        if image is None or image.shape[:2] != (720, 1280) or image.dtype != np.uint8:
            raise ValueError("expected decoded uint8 1280x720 frame")
        corners, ids, _ = self.detector.detectMarkers(image)
        if ids is None:
            return report | {"reason": "no_tags"}
        identifiers = ids.flatten().tolist()
        if len(set(identifiers)) != len(identifiers) or any(
            i not in self.tags for i in identifiers
        ):
            return report | {"reason": "unknown_or_duplicate_tag"}
        # ArUco returns decoded TL/TR/BR/BL; these are not image-position sorting.
        pixels = np.concatenate([c.reshape(4, 2) for c in corners]).astype(float)
        points = np.concatenate([map_points(self.tags[i]) for i in identifiers])
        centered = points - points.mean(axis=0)
        _, singular, axes = np.linalg.svd(centered)
        planar = singular[-1] < 1e-6
        if planar:
            basis = axes.T
            if np.linalg.det(basis) < 0:
                basis[:, 2] *= -1
            local = centered @ basis
            local[:, 2] = 0
            result = cv2.solvePnPGeneric(local, pixels, self.K, self.dist, flags=cv2.SOLVEPNP_IPPE)
        else:
            result = cv2.solvePnPGeneric(
                points, pixels, self.K, self.dist, flags=cv2.SOLVEPNP_SQPNP
            )
        candidates = []
        for rvec, tvec in zip(result[1], result[2], strict=True):
            rotation = cv2.Rodrigues(rvec)[0]
            translation = tvec.reshape(3)
            if planar:
                rotation = rotation @ basis.T
                translation = translation - rotation @ points.mean(axis=0)
            camera_points = points @ rotation.T + translation
            if np.min(camera_points[:, 2]) <= 0:
                continue
            T_camera_map = np.eye(4)
            T_camera_map[:3, :3], T_camera_map[:3, 3] = rotation, translation
            T_map_camera = np.linalg.inv(T_camera_map)
            if any(
                np.dot(
                    T_map_camera[:3, 3] - np.array(self.tags[i]["T_map_tag"])[:3, 3],
                    np.array(self.tags[i]["T_map_tag"])[:3, 2],
                )
                <= 0
                for i in identifiers
            ):
                continue
            projected = cv2.projectPoints(
                points, cv2.Rodrigues(rotation)[0], translation, self.K, self.dist
            )[0].reshape(-1, 2)
            error = float(np.sqrt(np.mean(np.sum((projected - pixels) ** 2, axis=1))))
            if np.isfinite(error):
                candidates.append((error, T_map_camera))
        candidates.sort(key=lambda item: item[0])
        if not candidates or candidates[0][0] > 2:
            return report | {"reason": "reprojection_or_cheirality"}
        if len(candidates) > 1 and (
            candidates[1][0] - candidates[0][0] < 0.5
            or candidates[1][0] < 2 * max(candidates[0][0], 1e-9)
        ):
            return report | {"reason": "ambiguous"}
        error, camera = candidates[0]
        body = camera @ np.linalg.inv(self.T_body_camera)
        return report | dict(
            accepted=True,
            reason="pose",
            tag_ids=identifiers,
            reprojection_rms_px=error,
            T_map_camera=camera.tolist(),
            T_map_body=body.tolist(),
        )
