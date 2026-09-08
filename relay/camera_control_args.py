"""Exact console camera intents; existing signed node commands remain unchanged."""

from collections.abc import Mapping

CAMERA_CONTROL_CAPABILITY = "camera_control_v1"


def camera_control_arguments(raw: object) -> dict[str, int | str]:
    if not isinstance(raw, Mapping):
        raise ValueError("camera_control args must be an object")
    if (
        isinstance(raw.get("kind"), str)
        and raw.get("kind") in {"ready", "photo"}
        and set(raw) == {"kind"}
    ):
        return dict(raw)
    if raw.get("kind") == "gimbal" and set(raw) == {"kind", "pitch_mdeg"}:
        if type(raw["pitch_mdeg"]) is int and -180_000 <= raw["pitch_mdeg"] <= 180_000:
            return dict(raw)
    raise ValueError("camera_control requires ready, photo, or bounded integer gimbal pitch_mdeg")
