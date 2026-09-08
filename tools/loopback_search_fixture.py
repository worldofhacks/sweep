"""Explicit synthetic camera inputs for the loopback search rehearsal."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from perception.object_detection import DEFAULT_TARGET_LABELS, DetectionCandidate
from perception.search_events import CameraPolicy
from planner.navigation import NavigationPermission
from planner.navigation_deployment import NavigationDeployment
from planner.search import SearchArea
from relay.search_detection import (
    CameraCalibrationConfig,
    DetectionSourceConfig,
    SearchDetectionConfig,
)
from relay.search_runtime import SearchRuntimeConfig

SYNTHETIC_SOURCE_ID = "synthetic-loopback-camera-1"
_SYNTHETIC_DETECTOR_SHA256 = "c3c1fe6d89d5325d1c7ff97fed0944f9dfca3630281cc2bf858e78a24b5ab9d8"


class SyntheticFrameStream:
    def __init__(self, _stream_url: str) -> None:
        self._closed = False
        self._frame = np.zeros((720, 1280, 3), dtype=np.uint8)

    def start(self) -> SyntheticFrameStream:
        if self._closed:
            raise RuntimeError("synthetic frame stream is closed")
        return self

    def read(self, _timeout: float) -> tuple[np.ndarray, float] | None:
        if self._closed:
            return None
        return self._frame, time.monotonic()

    def close(self) -> None:
        self._closed = True


class SyntheticDetector:
    target_labels = DEFAULT_TARGET_LABELS
    detector_config_sha256 = _SYNTHETIC_DETECTOR_SHA256

    @staticmethod
    def detect(_frame: np.ndarray) -> Sequence[DetectionCandidate]:
        return ()


def synthetic_detector(_source: DetectionSourceConfig) -> SyntheticDetector:
    return SyntheticDetector()


def synthetic_lobby_search_configuration(
    deployment: NavigationDeployment,
) -> tuple[SearchRuntimeConfig, SearchDetectionConfig]:
    artifact = deployment.artifact()
    lobby = next((zone for zone in artifact.zones if zone.zone_id == "lobby"), None)
    pins = deployment.config.frame(1).control_pins
    if (
        lobby is None
        or not lobby.owner_approved
        or 1 not in deployment.wire_profiles
        or pins is None
    ):
        raise ValueError("the loopback search fixture requires its approved lobby and drone 1")
    source = DetectionSourceConfig(
        1,
        SYNTHETIC_SOURCE_ID,
        "rtsp://synthetic.invalid/loopback-search-camera-1",
        Path("synthetic-loopback-search.onnx"),
        _SYNTHETIC_DETECTOR_SHA256,
        CameraCalibrationConfig(
            ((800, 0, 640), (0, 800, 360), (0, 0, 1)),
            ((1, 0, 0, 0), (0, -1, 0, 0), (0, 0, -1, 0), (0, 0, 0, 1)),
        ),
    )
    search = SearchRuntimeConfig(
        {"lobby": SearchArea("lobby", lobby.floor_id, lobby.polygon_xy, lobby.z_min_m)},
        artifact.map_pin,
        CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
        pins.camera_calibration_id,
        {1: SYNTHETIC_SOURCE_ID},
        NavigationPermission(frozenset({"lobby"})),
        maximum_drones=1,
    )
    return search, SearchDetectionConfig({1: source})
