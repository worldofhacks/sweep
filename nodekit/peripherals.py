"""Exact, bounded robot peripheral arguments shared by console-facing relay and nodes."""

from collections.abc import Mapping

PERIPHERAL_CAPABILITY = "robot_peripheral_v1"
PERIPHERAL_KINDS = frozenset({"neck", "speech", "lights", "screen"})
MAX_PERIPHERAL_TEXT = 240


def peripheral_arguments(raw: object) -> dict[str, int | str]:
    if not isinstance(raw, Mapping):
        raise ValueError("robot_peripheral args must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str):
        raise ValueError("peripheral kind must be a string")
    if kind == "neck" and set(raw) == {"kind", "position"}:
        if type(raw["position"]) is int and 300 <= raw["position"] <= 650:
            return dict(raw)
        raise ValueError("neck position must be an integer from 300 through 650")
    if kind == "lights" and set(raw) == {"kind", "h", "s", "v"}:
        if all(type(raw[key]) is int and 0 <= raw[key] <= 255 for key in ("h", "s", "v")):
            return dict(raw)
        raise ValueError("light HSV values must be integers from 0 through 255")
    if kind in {"speech", "screen"} and set(raw) == {"kind", "text"}:
        text = raw["text"]
        if (
            isinstance(text, str)
            and len(text) <= MAX_PERIPHERAL_TEXT
            and (text or kind == "screen")
            and (not text or text.isprintable())
            and text == text.strip()
        ):
            return dict(raw)
        raise ValueError(
            "peripheral text must be canonical printable text of at most 240 characters"
        )
    raise ValueError("unknown peripheral or arguments outside its exact contract")


def valid_peripheral_arguments(raw: object) -> bool:
    try:
        peripheral_arguments(raw)
    except (ValueError, TypeError):
        return False
    return True
