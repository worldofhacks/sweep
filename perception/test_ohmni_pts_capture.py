from __future__ import annotations

import io
from fractions import Fraction

import av
import numpy as np
import pytest

from perception.ohmni_pts_capture import NutCaptureReader, PtsCaptureError, capture_time_ns


def _nut_with_pts(values: tuple[int, ...]) -> io.BytesIO:
    output = io.BytesIO()
    with av.open(output, "w", format="nut") as container:
        stream = container.add_stream("ffv1", rate=2)
        stream.width, stream.height, stream.pix_fmt = 16, 16, "yuv420p"
        stream.time_base = Fraction(1, 1_000_000_000)
        for index, value in enumerate(values):
            frame = av.VideoFrame.from_ndarray(
                np.full((16, 16, 3), index * 50, np.uint8), format="bgr24"
            )
            frame.pts = value
            frame.time_base = Fraction(1, 1_000_000_000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    output.seek(0)
    return output


def test_nut_reader_preserves_the_encoded_nonzero_source_pts() -> None:
    frames = list(NutCaptureReader(_nut_with_pts((11_000_000_000, 11_500_000_000))).frames())

    assert [frame.capture_time_ns for frame in frames] == [11_000_000_000, 11_500_000_000]
    assert all(frame.image_bgr8.shape == (16, 16, 3) for frame in frames)


def test_nut_reader_refuses_nonmonotonic_capture_pts(monkeypatch: pytest.MonkeyPatch) -> None:
    class Frame:
        def __init__(self, pts: int) -> None:
            self.pts = pts
            self.time_base = Fraction(1, 1_000_000_000)

        def to_ndarray(self, *, format: str) -> np.ndarray:
            assert format == "bgr24"
            return np.zeros((16, 16, 3), np.uint8)

    class Container:
        streams = [type("Stream", (), {"type": "video"})()]

        def decode(self, _stream):
            return iter((Frame(11_000_000_000), Frame(11_000_000_000)))

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "perception.ohmni_pts_capture.av.open", lambda *_args, **_kwargs: Container()
    )
    with pytest.raises(PtsCaptureError, match="strictly increase"):
        list(NutCaptureReader(io.BytesIO()).frames())


@pytest.mark.parametrize(
    ("pts", "time_base"),
    [
        (None, Fraction(1, 1_000_000_000)),
        (1, Fraction(1, 3)),
        (-1, Fraction(1, 1_000_000_000)),
    ],
)
def test_capture_timestamp_requires_an_integral_nonnegative_nanosecond_value(
    pts: object, time_base: object
) -> None:
    with pytest.raises(PtsCaptureError):
        capture_time_ns(pts, time_base)
