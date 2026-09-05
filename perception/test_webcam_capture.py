import json

import cv2
import numpy as np
import pytest

from perception import webcam_capture


def test_capture_saves_decoded_pixels_and_receipt_times_without_capture_claim(
    tmp_path, monkeypatch
):
    class Source:
        def __init__(self, url):
            self.timestamp = 10

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, timeout):
            self.timestamp += 1
            return np.full((720, 1280, 3), 73, np.uint8), self.timestamp

    monkeypatch.setattr(webcam_capture, "WebcamStream", Source)
    monkeypatch.setattr(webcam_capture.time, "monotonic", lambda: 10)
    output = tmp_path / "frames"
    result = webcam_capture.collect("rtsp://localhost/drone1", output, count=2)
    assert result["complete"] is True
    rows = [json.loads(line) for line in (output / "decode-times.jsonl").read_text().splitlines()]
    assert [row["decode_monotonic_s"] for row in rows] == [11, 12]
    assert all(row["capture_time_verified"] is False for row in rows)
    assert np.all(cv2.imread(str(output / rows[0]["file"])) == 73)
    with pytest.raises(FileExistsError):
        webcam_capture.collect("rtsp://localhost/drone1", output, count=2)
