"""MediaMTX stream projection and measured startup evidence."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import ceil
from typing import Any

import httpx


def stream_path(drone_id: object) -> str:
    if isinstance(drone_id, bool) or not isinstance(drone_id, int) or not 1 <= drone_id <= 6:
        raise ValueError("drone_id must be an integer from 1 through 6")
    return f"drone{drone_id}"


@dataclass(slots=True)
class _Progress:
    bytes_received: int
    last_frame_at: int


class StreamHealthTracker:
    def __init__(self, *, stale_after_ms: int) -> None:
        if stale_after_ms <= 0:
            raise ValueError("stale_after_ms must be positive")
        self.stale_after_ms = stale_after_ms
        self._progress: dict[int, _Progress] = {}

    def project(
        self,
        *,
        drone_id: int,
        api_path: dict[str, Any] | None,
        observed_at_ms: int,
    ) -> dict[str, object]:
        path = stream_path(drone_id)
        progress = self._progress.get(drone_id)
        if api_path is None or api_path.get("name") != path or api_path.get("ready") is not True:
            return self._unavailable(progress)

        bytes_received = _nonnegative_integer(api_path.get("bytesReceived", 0))
        if bytes_received > 0 and (progress is None or bytes_received != progress.bytes_received):
            progress = _Progress(bytes_received=bytes_received, last_frame_at=observed_at_ms)
            self._progress[drone_id] = progress
            return {"status": "live", "last_frame_at": observed_at_ms}
        if progress is None:
            return {"status": "unreported", "last_frame_at": None}
        if observed_at_ms - progress.last_frame_at > self.stale_after_ms:
            return {"status": "offline", "last_frame_at": progress.last_frame_at}
        return {"status": "live", "last_frame_at": progress.last_frame_at}

    @staticmethod
    def _unavailable(progress: _Progress | None) -> dict[str, object]:
        if progress is None:
            return {"status": "unreported", "last_frame_at": None}
        return {"status": "offline", "last_frame_at": progress.last_frame_at}


class MediaMtxObserver:
    def __init__(
        self,
        *,
        api_url: str,
        username: str,
        password: str,
        stale_after_ms: int,
        fetch_json: Callable[[str, tuple[str, str]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        if not api_url or not username or not password:
            raise ValueError("MediaMTX API URL and credentials are required")
        self.api_url = api_url.rstrip("/")
        self.credential = (username, password)
        self.health = StreamHealthTracker(stale_after_ms=stale_after_ms)
        self.fetch_json = fetch_json or _fetch_json

    async def observe(self, *, observed_at_ms: int) -> dict[int, dict[str, object]]:
        try:
            payload = await self.fetch_json(f"{self.api_url}/v3/paths/list", self.credential)
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise ValueError("MediaMTX paths response omitted its items list")
            paths = {
                item.get("name"): item
                for item in items
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
        except (httpx.HTTPError, ValueError):
            paths = {}
        return {
            drone_id: self.health.project(
                drone_id=drone_id,
                api_path=paths.get(stream_path(drone_id)),
                observed_at_ms=observed_at_ms,
            )
            for drone_id in range(1, 7)
        }


def summarize_frame_latency(samples: list[dict[str, int]], *, protocol: str) -> dict[str, object]:
    if protocol not in {"whep", "hls"}:
        raise ValueError("protocol must be whep or hls")
    latencies: list[int] = []
    for sample in samples:
        source = sample.get("source_timestamp_ms")
        rendered = sample.get("rendered_timestamp_ms")
        if not isinstance(source, int) or not isinstance(rendered, int) or rendered < source:
            raise ValueError("each sample needs ordered integer source and render timestamps")
        latencies.append(rendered - source)
    if not latencies:
        raise ValueError("at least one latency sample is required")
    latencies.sort()
    p50 = latencies[ceil(len(latencies) * 0.50) - 1]
    p95 = latencies[ceil(len(latencies) * 0.95) - 1]
    target = 300 if protocol == "whep" else None
    return {
        "v": 1,
        "protocol": protocol,
        "sample_count": len(latencies),
        "latency_ms": latencies,
        "p50_ms": p50,
        "p95_ms": p95,
        "max_ms": latencies[-1],
        "target_ms": target,
        "target_met": p95 < target if target is not None else None,
    }


def webcam_acceptance_template(*, created_at_ms: int) -> dict[str, object]:
    return {
        "v": 1,
        "status": "pending",
        "reason": "awaiting_webcam_run",
        "created_at": created_at_ms,
        "source": "laptop_webcam",
        "stream": "drone1",
        "whep": {"target_ms": 300, "samples": [], "report": None},
        "hls": {"target_ms": None, "samples": [], "report": None},
        "recording": None,
    }


@dataclass(slots=True)
class LatencyTrace:
    source_started_at_ms: int
    path_ready_at_ms: int | None = None
    hls_frame_decoded_at_ms: int | None = None

    def mark_path_ready(self, observed_at_ms: int) -> None:
        self.path_ready_at_ms = self._validate(observed_at_ms)

    def mark_hls_frame_decoded(self, observed_at_ms: int) -> None:
        self.hls_frame_decoded_at_ms = self._validate(observed_at_ms)

    def report(self, *, stream: str) -> dict[str, object]:
        return {
            "v": 1,
            "stream": stream,
            "source_started_at": self.source_started_at_ms,
            "path_ready_at": self.path_ready_at_ms,
            "hls_frame_decoded_at": self.hls_frame_decoded_at_ms,
            "publish_to_path_ready_ms": self._duration(self.path_ready_at_ms),
            "publish_to_hls_frame_ms": self._duration(self.hls_frame_decoded_at_ms),
        }

    def _validate(self, observed_at_ms: int) -> int:
        if observed_at_ms < self.source_started_at_ms:
            raise ValueError("observation occurred before source start")
        return observed_at_ms

    def _duration(self, observed_at_ms: int | None) -> int | None:
        if observed_at_ms is None:
            return None
        return observed_at_ms - self.source_started_at_ms


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


async def _fetch_json(url: str, credential: tuple[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(auth=credential, timeout=2) as client:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("MediaMTX API response must be an object")
    return payload
