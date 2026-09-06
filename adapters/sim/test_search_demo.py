from __future__ import annotations

import numpy as np

from adapters.sim.search_demo import search_demo


def test_search_demo_has_a_pinned_synthetic_detection_source() -> None:
    demo = search_demo()
    stream = demo.stream_factory("rtsp://synthetic.local/search-camera-1")

    assert demo.publish_frame()
    frame = stream.read(0)

    assert frame is not None
    image, _ = frame
    assert image.shape == (720, 1280, 3)
    assert image.dtype == np.uint8
    detection = demo.config.search_detection
    assert detection is not None
    assert demo.detector_factory(detection.sources_by_drone[1]).detect(image)[0].label == "person"

    stream.close()
    next_stream = demo.stream_factory("rtsp://synthetic.local/search-camera-1")
    assert demo.publish_frame()

    assert next_stream is not stream
    assert next_stream.read(0) is not None
