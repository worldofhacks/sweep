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


def map_points(tag, *, world=False):
    transform = rigid(tag["T_world_tag" if world else "T_map_tag"])
    size = tag["size_m" if world else "size"]
    return tag_corners(size) @ transform[:3, :3].T + transform[:3, 3]


def _consensus_config(value):
    if value is None:
        return 1, 1, None, None
    if not isinstance(value, dict) or set(value) != {
        "minimum_distinct_tags",
        "maximum_candidate_tags",
        "maximum_translation_residual_m",
        "maximum_rotation_residual_rad",
    }:
        raise ValueError("tag consensus configuration is invalid")
    minimum = value["minimum_distinct_tags"]
    maximum = value["maximum_candidate_tags"]
    translation = value["maximum_translation_residual_m"]
    rotation = value["maximum_rotation_residual_rad"]
    if (
        type(minimum) is not int
        or not 1 <= minimum <= 6
        or type(maximum) is not int
        or not minimum <= maximum <= 6
        or type(translation) not in (int, float)
        or not np.isfinite(translation)
        or translation <= 0
        or type(rotation) not in (int, float)
        or not np.isfinite(rotation)
        or not 0 < rotation <= np.pi
    ):
        raise ValueError("tag consensus configuration is invalid")
    return minimum, maximum, float(translation), float(rotation)


def _rotation_residual(first, second):
    delta = first.T @ second
    return float(np.arccos(np.clip((np.trace(delta) - 1) / 2, -1, 1)))


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
        consensus=None,
    ):
        self.manifest = validate_bundle(bundle, accepted_versions)
        self.world = self.manifest["schema_version"] == 2
        self.pose_frame = {
            "name": self.manifest["frame"]["name"],
            "bundle_version": self.manifest["bundle_version"],
            "content_sha256": self.manifest["content_sha256"],
        }
        if self.world:
            self.pose_frame.update(
                map_id=self.manifest["map_id"],
                physical_datum=self.manifest["frame"]["physical_datum"],
                axis_convention=self.manifest["frame"]["axis_convention"],
            )
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
        (
            self.minimum_consensus_tags,
            self.maximum_consensus_candidates,
            self.maximum_translation_residual_m,
            self.maximum_rotation_residual_rad,
        ) = _consensus_config(consensus)
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
            pose_frame=dict(self.pose_frame),
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
            identifier not in self.tags for identifier in identifiers
        ):
            return report | {"reason": "unknown_or_duplicate_tag"}
        if self.world and any(
            not self.tags[identifier]["verified_for_flight"] for identifier in identifiers
        ):
            return report | {"reason": "unverified_world_tag", "tag_ids": identifiers}
        if self.minimum_consensus_tags > 1 and len(identifiers) > self.maximum_consensus_candidates:
            return report | {
                "reason": "too_many_consensus_tags",
                "tag_ids": identifiers,
                "maximum_candidate_tags": self.maximum_consensus_candidates,
            }
        pixels_by_id = {
            identifier: corner.reshape(4, 2).astype(float)
            for identifier, corner in zip(identifiers, corners, strict=True)
        }
        inlier_ids, consensus_reason, consensus = self._consensus(identifiers, pixels_by_id)
        if inlier_ids is None:
            return report | {
                "reason": consensus_reason,
                "tag_ids": identifiers,
                **consensus,
            }
        points = np.concatenate(
            [map_points(self.tags[identifier], world=self.world) for identifier in inlier_ids]
        )
        pixels = np.concatenate([pixels_by_id[identifier] for identifier in inlier_ids])
        camera, error, reason = self._camera_pose(points, pixels, inlier_ids)
        if camera is None:
            return report | {"reason": reason, "tag_ids": identifiers, **consensus}
        body = camera @ np.linalg.inv(self.T_body_camera)
        return (
            report
            | dict(
                accepted=True,
                reason="pose",
                tag_ids=identifiers,
                reprojection_rms_px=error,
                **consensus,
            )
            | {
                "T_world_camera" if self.world else "T_map_camera": camera.tolist(),
                "T_world_body" if self.world else "T_map_body": body.tolist(),
            }
        )

    def _consensus(self, identifiers, pixels_by_id):
        if self.minimum_consensus_tags == 1:
            return list(identifiers), None, {"consensus_status": "not_required"}
        candidates = {}
        for identifier in identifiers:
            points = map_points(self.tags[identifier], world=self.world)
            viable = [
                candidate
                for candidate in self._pose_candidates(
                    points, pixels_by_id[identifier], [identifier]
                )
                if candidate[0] <= 2
            ]
            if viable:
                candidates[identifier] = viable[:2]
        selected, ambiguous = self._largest_pairwise_consensus(candidates)
        inliers = [] if selected is None else sorted(candidate[0] for candidate in selected)
        by_id = {identifier: candidates[identifier][0] for identifier in candidates}
        if selected:
            by_id.update({candidate[0]: (candidate[1], candidate[2]) for candidate in selected})
        if selected:
            reference = min(
                selected,
                key=lambda candidate: (
                    sum(
                        np.linalg.norm(candidate[2][:3, 3] - other[2][:3, 3])
                        + _rotation_residual(candidate[2][:3, :3], other[2][:3, :3])
                        for other in selected
                    ),
                    candidate[0],
                    candidate[1],
                ),
            )
            reference_id, _, reference_camera, _ = reference
        else:
            reference_id, reference_camera = None, None
        residuals = [
            {
                "tag_id": identifier,
                "reprojection_rms_px": by_id[identifier][0],
                "translation_m": None
                if reference_camera is None
                else float(np.linalg.norm(by_id[identifier][1][:3, 3] - reference_camera[:3, 3])),
                "rotation_rad": None
                if reference_camera is None
                else _rotation_residual(by_id[identifier][1][:3, :3], reference_camera[:3, :3]),
            }
            for identifier in sorted(candidates)
        ]
        diagnostics = {
            "consensus_status": "ambiguous"
            if ambiguous
            else "accepted"
            if len(inliers) >= self.minimum_consensus_tags
            else "not_reached",
            "consensus_required_distinct_tags": self.minimum_consensus_tags,
            "consensus_maximum_candidate_tags": self.maximum_consensus_candidates,
            "consensus_candidate_tag_ids": sorted(candidates),
            "consensus_inlier_tag_ids": inliers,
            "consensus_outlier_tag_ids": sorted(set(identifiers) - set(inliers)),
            "consensus_reference_tag_id": reference_id,
            "consensus_pairwise_compatible": bool(selected),
            "consensus_residuals": residuals,
        }
        if ambiguous:
            return None, "ambiguous_consensus", diagnostics
        if len(inliers) < self.minimum_consensus_tags:
            return None, "insufficient_tag_consensus", diagnostics
        return inliers, None, diagnostics

    def _largest_pairwise_consensus(self, candidates):
        groups = [(identifier, candidates[identifier]) for identifier in sorted(candidates)]
        largest = 0
        best_by_tag_set = {}

        def compatible(first, second):
            return (
                np.linalg.norm(first[2][:3, 3] - second[2][:3, 3])
                <= self.maximum_translation_residual_m
                and _rotation_residual(first[2][:3, :3], second[2][:3, :3])
                <= self.maximum_rotation_residual_rad
            )

        def visit(index, selected):
            nonlocal largest, best_by_tag_set
            if len(selected) + len(groups) - index < largest:
                return
            if index == len(groups):
                size = len(selected)
                if not size:
                    return
                tag_set = tuple(candidate[0] for candidate in selected)
                score = tuple((candidate[1], candidate[0], candidate[3]) for candidate in selected)
                if size > largest:
                    largest, best_by_tag_set = size, {tag_set: (score, tuple(selected))}
                elif size == largest:
                    current = best_by_tag_set.get(tag_set)
                    if current is None or score < current[0]:
                        best_by_tag_set[tag_set] = (score, tuple(selected))
                return
            identifier, poses = groups[index]
            for pose_index, (error, camera) in enumerate(poses):
                candidate = (identifier, error, camera, pose_index)
                if all(compatible(candidate, existing) for existing in selected):
                    visit(index + 1, [*selected, candidate])
            visit(index + 1, selected)

        visit(0, [])
        if not best_by_tag_set:
            return None, False
        if len(best_by_tag_set) > 1 and largest >= self.minimum_consensus_tags:
            return None, True
        return min(best_by_tag_set.values(), key=lambda item: item[0])[1], False

    def _camera_pose(self, points, pixels, identifiers):
        candidates = self._pose_candidates(points, pixels, identifiers)
        if not candidates or candidates[0][0] > 2:
            return None, None, "reprojection_or_cheirality"
        if len(candidates) > 1 and (
            candidates[1][0] - candidates[0][0] < 0.5
            or candidates[1][0] < 2 * max(candidates[0][0], 1e-9)
        ):
            return None, None, "ambiguous"
        error, camera = candidates[0]
        return camera, error, None

    def _pose_candidates(self, points, pixels, identifiers):
        transform_key = "T_world_tag" if self.world else "T_map_tag"
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
                    T_map_camera[:3, 3] - np.array(self.tags[identifier][transform_key])[:3, 3],
                    np.array(self.tags[identifier][transform_key])[:3, 2],
                )
                <= 0
                for identifier in identifiers
            ):
                continue
            projected = cv2.projectPoints(
                points, cv2.Rodrigues(rotation)[0], translation, self.K, self.dist
            )[0].reshape(-1, 2)
            error = float(np.sqrt(np.mean(np.sum((projected - pixels) ** 2, axis=1))))
            if np.isfinite(error):
                candidates.append((error, T_map_camera))
        candidates.sort(key=lambda item: item[0])
        return candidates
