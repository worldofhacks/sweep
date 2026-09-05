from __future__ import annotations

import pytest

from calibration.latency import summarize_latency


def _summarize(samples: dict[str, object]) -> dict[str, object]:
    return summarize_latency(
        camera_serial="fixture-camera",
        pipeline={
            "resolution_px": [1280, 720],
            "codec": "h264",
            "decoder_path": "test-decoder",
            "camera_mode": "fpv",
            "android_device_id": "not_applicable",
            "network_id": "not_applicable",
        },
        evidence_kind="recorded_live",
        samples=samples,
    )


def test_declared_duration_alone_cannot_certify_a_capture() -> None:
    result = _summarize({"duration_ms": 60_000, "samples_ms": [120]})

    assert result["p95_ms"] == 120
    assert result["meets_60_second_capture_minimum"] is False
    assert result["observed_sample_span_ms"] is None


@pytest.mark.parametrize(
    ("times", "expected"),
    [
        ([0], False),
        ([0, 60_000], False),
        ([index * 1_000 for index in range(20)], False),
        ([index * 4_000 for index in range(20)], True),
        ([10_000 + index * 3_000 for index in range(20)], False),
        ([index * 3_000 for index in range(21)], True),
    ],
)
def test_capture_requires_sample_count_and_observed_span(times: list[int], expected: bool) -> None:
    result = _summarize(
        {"duration_ms": 90_000, "samples_ms": [120] * len(times), "sample_times_ms": times}
    )

    assert result["meets_60_second_capture_minimum"] is expected
    assert result["sample_times_ms"] == times
    assert result["observed_sample_span_ms"] == times[-1] - times[0]


@pytest.mark.parametrize(
    "times",
    [
        None,
        "0,60000",
        [],
        [0],
        [0, 0],
        [100, 0],
        [-1, 60_000],
        [0, 60_001],
        [False, 60_000],
        [0, "60000"],
        [0, float("nan")],
        [0, float("inf")],
    ],
)
def test_invalid_sample_timing_evidence_is_rejected(times: object) -> None:
    with pytest.raises(ValueError, match="sample_times_ms"):
        _summarize({"duration_ms": 60_000, "samples_ms": [100, 120], "sample_times_ms": times})
