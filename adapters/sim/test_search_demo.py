from __future__ import annotations

import numpy as np

from adapters.sim.search_demo import search_demo


def test_search_demo_has_a_pinned_synthetic_detection_source() -> None:
    demo = search_demo()

    demo.publish_frame()
    frame = demo.stream.read(0)

    assert frame is not None
    image, _ = frame
    assert image.shape == (720, 1280, 3)
    assert image.dtype == np.uint8
    detection = demo.config.search_detection
    assert detection is not None
    assert demo.detector_factory(detection.sources_by_drone[1]).detect(image)[0].label == "person"
