"""Decode a private NUT sidecar while preserving camera capture timestamps."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from typing import BinaryIO

import av
import numpy as np

_NANOSECONDS = 1_000_000_000
_MAX_CAPTURE_NS = 2**63 - 1


class PtsCaptureError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    image_bgr8: np.ndarray
    capture_time_ns: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.image_bgr8, np.ndarray)
            or self.image_bgr8.dtype != np.uint8
            or self.image_bgr8.ndim != 3
            or self.image_bgr8.shape[2] != 3
        ):
            raise PtsCaptureError("NUT frame must decode to BGR8")
        if (
            type(self.capture_time_ns) is not int
            or not 0 <= self.capture_time_ns <= _MAX_CAPTURE_NS
        ):
            raise PtsCaptureError("NUT capture timestamp is outside the int64 nanosecond range")


def capture_time_ns(pts: object, time_base: object) -> int:
    if type(pts) is not int or not isinstance(time_base, Fraction) or time_base <= 0:
        raise PtsCaptureError("NUT video frame has no positive integer PTS")
    value = Fraction(pts) * time_base * _NANOSECONDS
    if value.denominator != 1 or not 0 <= value.numerator <= _MAX_CAPTURE_NS:
        raise PtsCaptureError("NUT PTS cannot be represented as integral nanoseconds")
    return value.numerator


class NutCaptureReader:
    """Yield one video stream's BGR frames with their source PTS."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source

    def frames(self) -> Iterator[CapturedFrame]:
        try:
            container = av.open(self._source, mode="r", format="nut")
        except av.FFmpegError as error:
            raise PtsCaptureError("PTS sidecar is not a readable NUT stream") from error
        try:
            videos = [stream for stream in container.streams if stream.type == "video"]
            if len(videos) != 1:
                raise PtsCaptureError("PTS sidecar must contain exactly one video stream")
            previous: int | None = None
            for frame in container.decode(videos[0]):
                timestamp = capture_time_ns(frame.pts, frame.time_base)
                if previous is not None and timestamp <= previous:
                    raise PtsCaptureError("PTS sidecar capture timestamps must strictly increase")
                previous = timestamp
                yield CapturedFrame(frame.to_ndarray(format="bgr24"), timestamp)
        except av.FFmpegError as error:
            raise PtsCaptureError("PTS sidecar decode failed") from error
        finally:
            container.close()
