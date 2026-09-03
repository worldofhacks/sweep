"""Hardware-free replay metrics for the Mini 3 Android bridge."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from math import ceil
from pathlib import Path


class BenchHarness:
    """Replays timestamped bridge observations and produces the hardware report shape."""

    def __init__(self) -> None:
        self._command_rtts: list[float] = []
        self._command_sent_times: list[int] = []
        self._command_drops = 0
        self._rejections: dict[str, int] = {}
        self._telemetry_times: list[int] = []
        self._video_segments: list[tuple[float, float, float, float]] = []
        self._video_drops = 0
        self._phone_samples: list[tuple[float, bool, float]] = []

    def record_command_sent(self, *, sent_at_ms: int, round_trip_ms: int | float) -> None:
        self._require_timestamp(sent_at_ms, "sent_at_ms")
        if not isinstance(round_trip_ms, int | float) or isinstance(round_trip_ms, bool):
            raise ValueError("round_trip_ms must be a number")
        if round_trip_ms < 0:
            raise ValueError("round_trip_ms must be non-negative")
        self._command_sent_times.append(sent_at_ms)
        self._command_rtts.append(float(round_trip_ms))

    def record_command_rejection(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        self._rejections[reason] = self._rejections.get(reason, 0) + 1

    def record_telemetry(self, observed_at_ms: int) -> None:
        self._require_timestamp(observed_at_ms, "observed_at_ms")
        self._telemetry_times.append(observed_at_ms)

    def record_command_drop(self, count: int) -> None:
        self._command_drops += self._require_count(count)

    def record_video_frame(
        self,
        *,
        captured_at_ms: int,
        controller_at_ms: int,
        decoded_at_ms: int,
        delivered_at_ms: int,
    ) -> None:
        timestamps = (captured_at_ms, controller_at_ms, decoded_at_ms, delivered_at_ms)
        if any(not isinstance(value, int) or value < 0 for value in timestamps):
            raise ValueError("video timestamps must be non-negative integers")
        if tuple(sorted(timestamps)) != timestamps:
            raise ValueError("video timestamps must be ordered from capture to delivery")
        self._video_segments.append(
            (
                float(controller_at_ms - captured_at_ms),
                float(decoded_at_ms - controller_at_ms),
                float(delivered_at_ms - decoded_at_ms),
                float(delivered_at_ms - captured_at_ms),
            )
        )

    def record_video_drop(self, count: int) -> None:
        self._video_drops += self._require_count(count)

    def record_phone_sample(
        self, *, thermal_c: float, throttled: bool, battery_draw_ma: float
    ) -> None:
        if not isinstance(thermal_c, int | float) or isinstance(thermal_c, bool):
            raise ValueError("thermal_c must be a number")
        if not isinstance(throttled, bool):
            raise ValueError("throttled must be a boolean")
        if not isinstance(battery_draw_ma, int | float) or isinstance(battery_draw_ma, bool):
            raise ValueError("battery_draw_ma must be a number")
        self._phone_samples.append((float(thermal_c), throttled, float(battery_draw_ma)))

    def report(self) -> dict[str, object]:
        return {
            "virtual_stick_hz": self._command_hz(),
            "command_rtt_ms": {
                **self._measurement(self._command_rtts),
                "jitter_p95": _jitter(self._command_rtts),
                "dropped": self._command_drops,
            },
            "command_rejections": dict(sorted(self._rejections.items())),
            "telemetry_hz": self._telemetry_hz(),
            "video_latency_ms": self._video_latency(),
            "phone": self._phone_metrics(),
        }

    @staticmethod
    def _require_timestamp(value: int, field: str) -> None:
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")

    @staticmethod
    def _require_count(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("count must be a non-negative integer")
        return value

    @staticmethod
    def _measurement(values: list[float]) -> dict[str, object]:
        return {"count": len(values), "p95": _percentile(values, 0.95)}

    def _telemetry_hz(self) -> float | None:
        if len(self._telemetry_times) < 2:
            return None
        duration_ms = max(self._telemetry_times) - min(self._telemetry_times)
        if duration_ms <= 0:
            return None
        return round((len(self._telemetry_times) - 1) * 1_000 / duration_ms, 3)

    def _command_hz(self) -> float | None:
        if len(self._command_sent_times) < 2:
            return None
        duration_ms = max(self._command_sent_times) - min(self._command_sent_times)
        if duration_ms <= 0:
            return None
        return round((len(self._command_sent_times) - 1) * 1_000 / duration_ms, 3)

    def _video_latency(self) -> dict[str, float | None]:
        columns = (
            tuple(zip(*self._video_segments, strict=True))
            if self._video_segments
            else ((), (), (), ())
        )
        return {
            "aircraft_to_controller_p95": _percentile(list(columns[0]), 0.95),
            "android_processing_p95": _percentile(list(columns[1]), 0.95),
            "lan_delivery_p95": _percentile(list(columns[2]), 0.95),
            "glass_to_glass_p95": _percentile(list(columns[3]), 0.95),
            "dropped_frames": self._video_drops,
        }

    def _phone_metrics(self) -> dict[str, float | int | None]:
        if not self._phone_samples:
            return {"max_thermal_c": None, "throttled_samples": 0, "max_battery_draw_ma": None}
        return {
            "max_thermal_c": max(sample[0] for sample in self._phone_samples),
            "throttled_samples": sum(sample[1] for sample in self._phone_samples),
            "max_battery_draw_ma": max(sample[2] for sample in self._phone_samples),
        }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = ceil(quantile * len(ordered)) - 1
    return ordered[index]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay DJI Mini 3 bridge bench observations.")
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args(argv)
    harness = BenchHarness()
    lines = args.input.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            event = json.loads(line)
            _replay_event(harness, event)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid bench event on line {line_number}: {error}") from error
    print(json.dumps(harness.report(), sort_keys=True))
    return 0


def _replay_event(harness: BenchHarness, event: object) -> None:
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("event must be an object with a type")
    event_type = event["type"]
    if event_type == "command":
        harness.record_command_sent(
            sent_at_ms=event["sent_at_ms"], round_trip_ms=event["round_trip_ms"]
        )
    elif event_type == "command_rejection":
        harness.record_command_rejection(event["reason"])
    elif event_type == "telemetry":
        harness.record_telemetry(event["observed_at_ms"])
    elif event_type == "command_drop":
        harness.record_command_drop(event["count"])
    elif event_type == "video":
        harness.record_video_frame(
            captured_at_ms=event["captured_at_ms"],
            controller_at_ms=event["controller_at_ms"],
            decoded_at_ms=event["decoded_at_ms"],
            delivered_at_ms=event["delivered_at_ms"],
        )
    elif event_type == "phone":
        harness.record_phone_sample(
            thermal_c=event["thermal_c"],
            throttled=event["throttled"],
            battery_draw_ma=event["battery_draw_ma"],
        )
    elif event_type == "video_drop":
        harness.record_video_drop(event["count"])
    else:
        raise ValueError(f"unsupported event type {event_type!r}")


def _jitter(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return _percentile(
        [abs(current - previous) for previous, current in zip(values, values[1:], strict=False)],
        0.95,
    )


if __name__ == "__main__":
    raise SystemExit(main())
