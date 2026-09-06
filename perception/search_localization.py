"""Projects confirmed detector boxes onto approved map-floor search zones."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from perception.object_detection import DetectionCandidate, SightingEvent
from perception.search_events import FramePoseEvidence
from planner.navigation import Pose, Zone


@dataclass(frozen=True, slots=True)
class SearchCameraModel:
    intrinsics: tuple[tuple[float, float, float], ...]
    map_from_camera: tuple[tuple[float, float, float, float], ...]

    def __post_init__(self) -> None:
        matrix = np.asarray(self.intrinsics, dtype=float)
        transform = np.asarray(self.map_from_camera, dtype=float)
        if matrix.shape != (3, 3) or transform.shape != (4, 4):
            raise ValueError("camera model matrices have invalid dimensions")
        if not np.isfinite(matrix).all() or not np.isfinite(transform).all():
            raise ValueError("camera model matrices must be finite")
        rotation = transform[:3, :3]
        if (
            matrix[0, 0] <= 0
            or matrix[1, 1] <= 0
            or not np.allclose(matrix[2], (0, 0, 1), atol=1e-9)
            or abs(np.linalg.det(matrix)) < 1e-9
            or not np.allclose(transform[3], (0, 0, 0, 1), atol=1e-9)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(rotation), 1, atol=1e-6)
        ):
            raise ValueError("camera model is not projectable")


@dataclass(frozen=True, slots=True)
class SearchLocalization:
    pose: Pose
    samples: int


def _inside(x: float, y: float, polygon: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > y) != (previous[1] > y) and x < (
            (previous[0] - current[0]) * (y - current[1]) / (previous[1] - current[1]) + current[0]
        ):
            inside = not inside
        previous = current
    return inside


def project_bottom_center(
    candidate: DetectionCandidate,
    image_width_px: int,
    model: SearchCameraModel,
    zones: tuple[Zone, ...],
) -> Pose | None:
    if type(image_width_px) is not int or image_width_px <= 0 or not zones:
        raise ValueError("image width and approved zones are required")
    left, _, right, bottom = candidate.bbox_xyxy
    pixel = np.array([(left + right) / 2, bottom, 1.0])
    camera_ray = np.linalg.inv(np.asarray(model.intrinsics)) @ pixel
    rotation = np.asarray(model.map_from_camera)[:3, :3]
    origin = np.asarray(model.map_from_camera)[:3, 3]
    ray = rotation @ camera_ray
    for zone in zones:
        if abs(ray[2]) < 1e-9:
            continue
        distance = (zone.z_min_m - origin[2]) / ray[2]
        if distance <= 0:
            continue
        point = origin + distance * ray
        if _inside(float(point[0]), float(point[1]), zone.polygon_xy):
            return Pose(float(point[0]), float(point[1]), zone.z_min_m, zone.floor_id)
    return None


class FiveFrameLocalizer:
    def __init__(self, zones: tuple[Zone, ...]) -> None:
        if not zones or any(not zone.owner_approved for zone in zones):
            raise ValueError("only owner-approved search zones are allowed")
        self._zones = zones
        self._samples: deque[Pose] = deque(maxlen=5)
        self._candidate_id: str | None = None
        self._frames: deque[str] = deque(maxlen=512)

    def observe(self, pose: Pose | None, candidate_id: str = "") -> SearchLocalization | None:
        if pose is None:
            return None
        if self._candidate_id != candidate_id:
            self._candidate_id = candidate_id
            self._samples.clear()
            self._frames.clear()
        if not any(
            zone.floor_id == pose.floor_id and _inside(pose.x_m, pose.y_m, zone.polygon_xy)
            for zone in self._zones
        ):
            return None
        self._samples.append(pose)
        if len(self._samples) < 5:
            return None
        values = np.asarray([(item.x_m, item.y_m, item.z_m) for item in self._samples])
        x, y, z = np.median(values, axis=0)
        zone = next(
            (
                zone
                for zone in self._zones
                if zone.floor_id == pose.floor_id and _inside(float(x), float(y), zone.polygon_xy)
            ),
            None,
        )
        if zone is None:
            return None
        return SearchLocalization(Pose(float(x), float(y), float(z), zone.floor_id), 5)

    def observe_sighting(
        self,
        event: SightingEvent,
        evidence: FramePoseEvidence,
        model: SearchCameraModel,
        image_width_px: int,
        now_s: float,
        *,
        accepted_frame: bool,
    ) -> SearchLocalization | None:
        if (
            not accepted_frame
            or evidence.identity != event.identity
            or event.identity.frame_id in self._frames
            or not 0 <= now_s - event.evaluation_completed_at_monotonic_s <= 0.5
            or not 0 <= now_s - evidence.observed_at_s <= 0.5
            or abs(evidence.pose_timestamp_s - event.last_frame_decoded_at_monotonic_s) > 0.2
        ):
            return None
        pose = project_bottom_center(event.candidate, image_width_px, model, self._zones)
        result = self.observe(pose, event.sighting_id)
        self._frames.append(event.identity.frame_id)
        return result
