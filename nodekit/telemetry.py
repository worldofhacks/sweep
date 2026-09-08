"""Bounded JSON telemetry shared by node producers and relay validation.

Custom readings are informational. They never grant motion authority or replace
the signed readiness protocol. Missing hardware readings are represented by null.
"""

from __future__ import annotations

import json
import math
import re

MAX_DEVICE_TELEMETRY_BYTES = 16_384
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def device_telemetry_payload(value: object) -> dict[str, object]:
    """Validate and copy a JSON object, permitting four levels including root."""
    count = 0

    def copy(item: object, depth: int) -> object:
        nonlocal count
        count += 1
        if depth > 4 or count > 4096:
            raise ValueError("device_telemetry exceeds its nesting or value limit")
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, (int, float)):
            if abs(item) > 2**53 - 1 or not math.isfinite(item):
                raise ValueError("device_telemetry numbers must be finite and JSON-safe")
            return item
        if isinstance(item, str):
            if len(item) > 512:
                raise ValueError("device_telemetry string exceeds 512 characters")
            return item
        if isinstance(item, list):
            if len(item) > 512:
                raise ValueError("device_telemetry list exceeds 512 entries")
            return [copy(child, depth + 1) for child in item]
        if isinstance(item, dict):
            if len(item) > 128:
                raise ValueError("device_telemetry object exceeds 128 keys")
            if any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in item):
                raise ValueError("device_telemetry keys must use bounded snake_case names")
            return {key: copy(child, depth + 1) for key, child in item.items()}
        raise ValueError("device_telemetry must contain only JSON values")

    if not isinstance(value, dict):
        raise ValueError("device_telemetry must be an object")
    result = copy(value, 1)
    assert isinstance(result, dict)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_DEVICE_TELEMETRY_BYTES:
        raise ValueError("device_telemetry exceeds 16 KiB")
    return result
