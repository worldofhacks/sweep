"""Summarize manually measured video-pipeline latency samples."""

from __future__ import annotations

import json
from hashlib import sha256
from math import ceil, floor, isfinite

from calibration.intrinsics import _pipeline


def summarize_latency(
    *,
    camera_serial: str,
    pipeline: dict[str, object],
    evidence_kind: str,
    samples: dict[str, object],
) -> dict[str, object]:
    if not camera_serial.strip():
        raise ValueError("camera serial must not be empty")
    if evidence_kind not in {"synthetic", "recorded_live"}:
        raise ValueError("evidence kind must be synthetic or recorded_live")
    duration_ms = samples.get("duration_ms")
    values = samples.get("samples_ms")
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms <= 0:
        raise ValueError("samples duration_ms must be a positive integer")
    if not isinstance(values, list) or not values:
        raise ValueError("samples samples_ms must be a non-empty list")
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if len(numbers) != len(values) or any(not isfinite(value) or value < 0 for value in numbers):
        raise ValueError(
            "each latency sample must be a finite, non-negative number of milliseconds"
        )

    return {
        "schema_version": 1,
        "status": "offline",
        "evidence_kind": evidence_kind,
        "camera_serial": camera_serial,
        "pipeline": _pipeline(pipeline),
        "sample_count": len(numbers),
        "duration_ms": duration_ms,
        "samples_ms": numbers,
        "samples_sha256": sha256(
            json.dumps(samples, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest(),
        "p50_ms": _percentile(numbers, 50),
        "p95_ms": _percentile(numbers, 95),
        "meets_60_second_capture_minimum": duration_ms >= 60_000,
    }


def _percentile(values: list[float], percentile: int) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = floor(position)
    upper = ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
