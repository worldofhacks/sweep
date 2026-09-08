from __future__ import annotations

import numpy as np

from relay.tests.test_platform_navigation_execution import _deployment
from tools.loopback_search_fixture import (
    SYNTHETIC_SOURCE_ID,
    SyntheticFrameStream,
    synthetic_detector,
    synthetic_lobby_search_configuration,
)


def test_synthetic_lobby_search_fixture_uses_an_approved_lobby_and_empty_detector(tmp_path) -> None:
    deployment = _deployment(tmp_path)
    search, detection = synthetic_lobby_search_configuration(deployment)

    assert search.areas["lobby"].zone_id == "lobby"
    assert search.source_by_drone == {1: SYNTHETIC_SOURCE_ID}
    assert search.calibration_id == deployment.config.frame(1).control_pins.camera_calibration_id
    source = detection.sources_by_drone[1]
    assert source.source_id == SYNTHETIC_SOURCE_ID
    assert synthetic_detector(source).detect(np.zeros((8, 8, 3), dtype=np.uint8)) == ()

    stream = SyntheticFrameStream(source.stream_url).start()
    frame = stream.read(0)
    assert frame is not None
    assert frame[0].shape == (720, 1280, 3)
    assert frame[0].dtype == np.uint8
    stream.close()
    assert stream.read(0) is None
