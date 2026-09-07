"""Explicit bounded camera identities and safe local MediaMTX stream names."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from relay.fleet_limits import MAX_DEVICE_ID, MAX_FLEET_DEVICES

MAX_CAMERAS_PER_DEVICE = 8
MAX_MEDIA_STREAMS = MAX_FLEET_DEVICES * MAX_CAMERAS_PER_DEVICE
STREAM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
CAMERA_ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


def valid_stream_name(value: object) -> bool:
    return isinstance(value, str) and STREAM_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class CameraStream:
    camera_id: str
    label: str
    stream: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.camera_id, str)
            or CAMERA_ID_PATTERN.fullmatch(self.camera_id) is None
            or not isinstance(self.label, str)
            or not 1 <= len(self.label) <= 64
            or not self.label.isprintable()
            or self.label != self.label.strip()
            or not valid_stream_name(self.stream)
        ):
            raise ValueError("camera identity, label or stream is outside the bounded contract")

    def to_dict(self) -> dict[str, str]:
        return {"camera_id": self.camera_id, "label": self.label, "stream": self.stream}


def validate_camera_mapping(
    cameras: Mapping[int, tuple[CameraStream, ...]], configured_ids: set[int]
) -> dict[int, tuple[CameraStream, ...]]:
    if not isinstance(cameras, Mapping) or len(cameras) > MAX_FLEET_DEVICES:
        raise ValueError("camera mapping exceeds the configured fleet bound")
    result = {}
    streams: set[str] = set()
    for device_id, entries in cameras.items():
        if (
            type(device_id) is not int
            or not 1 <= device_id <= MAX_DEVICE_ID
            or device_id not in configured_ids
        ):
            raise ValueError("camera mapping must name a configured device ID")
        if not isinstance(entries, tuple) or len(entries) > MAX_CAMERAS_PER_DEVICE:
            raise ValueError("a device may configure at most eight cameras")
        camera_ids: set[str] = set()
        for camera in entries:
            if not isinstance(camera, CameraStream):
                raise ValueError("camera entries must be CameraStream values")
            if camera.camera_id in camera_ids or camera.stream in streams:
                raise ValueError(
                    "camera IDs must be unique per device and stream names unique globally"
                )
            camera_ids.add(camera.camera_id)
            streams.add(camera.stream)
        result[device_id] = entries
    return result


def parse_camera_mapping(
    raw: object, configured_ids: set[int]
) -> dict[int, tuple[CameraStream, ...]]:
    if not isinstance(raw, Mapping) or len(raw) > MAX_FLEET_DEVICES:
        raise ValueError("camera configuration must be an object keyed by device ID")
    cameras = {}
    for key, entries in raw.items():
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 10
            or not key.isascii()
            or not key.isdecimal()
            or str(int(key)) != key
        ):
            raise ValueError("camera configuration keys must be canonical device IDs")
        if not isinstance(entries, list) or len(entries) > MAX_CAMERAS_PER_DEVICE:
            raise ValueError("camera configuration values must be lists of at most eight entries")
        if any(
            not isinstance(entry, Mapping) or set(entry) != {"camera_id", "label", "stream"}
            for entry in entries
        ):
            raise ValueError("camera entries require exactly camera_id, label and stream")
        cameras[int(key)] = tuple(CameraStream(**entry) for entry in entries)
    return validate_camera_mapping(cameras, configured_ids)
