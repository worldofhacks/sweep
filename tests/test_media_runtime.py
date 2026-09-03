from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from relay.app import RelayRuntime
from relay.contracts import parse_membership_request
from relay.settings import RelaySettings
from relay.tests.conftest import CONSOLE_KEY, membership_payload

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "media" / "runtime.py"
SPEC = importlib.util.spec_from_file_location("sweep_media_runtime", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


def test_stream_path_accepts_only_declared_drone_ids() -> None:
    assert runtime.stream_path(1) == "drone1"
    assert runtime.stream_path(6) == "drone6"

    for invalid in (0, 7, True, "1"):
        with pytest.raises(ValueError, match="integer from 1 through 6"):
            runtime.stream_path(invalid)


def test_stream_health_is_unreported_until_bytes_arrive() -> None:
    tracker = runtime.StreamHealthTracker(stale_after_ms=1_000)

    assert tracker.project(drone_id=3, api_path=None, observed_at_ms=100) == {
        "status": "unreported",
        "last_frame_at": None,
    }


def test_stream_health_tracks_progress_loss_and_recovery_without_urls() -> None:
    tracker = runtime.StreamHealthTracker(stale_after_ms=1_000)
    ready = {"name": "drone3", "ready": True, "bytesReceived": 1200, "tracks": ["H264"]}

    first = tracker.project(drone_id=3, api_path=ready, observed_at_ms=1_000)
    unchanged = tracker.project(drone_id=3, api_path=ready, observed_at_ms=1_900)
    stale = tracker.project(drone_id=3, api_path=ready, observed_at_ms=2_001)
    recovered = tracker.project(
        drone_id=3,
        api_path={**ready, "bytesReceived": 1400},
        observed_at_ms=2_100,
    )

    assert first == {"status": "live", "last_frame_at": 1_000}
    assert unchanged == {"status": "live", "last_frame_at": 1_000}
    assert stale == {"status": "offline", "last_frame_at": 1_000}
    assert recovered == {"status": "live", "last_frame_at": 2_100}
    assert "url" not in json.dumps(recovered)


def test_stream_health_marks_a_previously_seen_unready_path_offline() -> None:
    tracker = runtime.StreamHealthTracker(stale_after_ms=1_000)
    tracker.project(
        drone_id=1,
        api_path={"name": "drone1", "ready": True, "bytesReceived": 100},
        observed_at_ms=100,
    )

    projection = tracker.project(
        drone_id=1,
        api_path={"name": "drone1", "ready": False, "bytesReceived": 100},
        observed_at_ms=200,
    )

    assert projection == {"status": "offline", "last_frame_at": 100}


def test_stream_health_does_not_assign_cross_path_progress() -> None:
    tracker = runtime.StreamHealthTracker(stale_after_ms=1_000)

    projection = tracker.project(
        drone_id=1,
        api_path={"name": "drone2", "ready": True, "bytesReceived": 20},
        observed_at_ms=100,
    )

    assert projection == {"status": "unreported", "last_frame_at": None}


def test_mediamtx_observer_projects_api_progress_without_playback_urls() -> None:
    payloads = iter(
        [
            {"items": [{"name": "drone1", "ready": True, "bytesReceived": 120}]},
            {"items": [{"name": "drone1", "ready": False, "bytesReceived": 120}]},
        ]
    )

    async def fetch(_url: str, _credential: tuple[str, str]) -> dict[str, object]:
        return next(payloads)

    observer = runtime.MediaMtxObserver(
        api_url="http://127.0.0.1:9997",
        username="admin",
        password="secret",
        stale_after_ms=1_000,
        fetch_json=fetch,
    )

    async def exercise() -> tuple[dict[int, dict[str, object]], dict[int, dict[str, object]]]:
        return (
            await observer.observe(observed_at_ms=1_000),
            await observer.observe(observed_at_ms=1_100),
        )

    live, lost = asyncio.run(exercise())

    assert live[1] == {"status": "live", "last_frame_at": 1_000}
    assert lost[1] == {"status": "offline", "last_frame_at": 1_000}
    assert "url" not in json.dumps(live)


def test_relay_runtime_publishes_observed_media_health(tmp_path: Path) -> None:
    class Observer:
        async def observe(self, *, observed_at_ms: int) -> dict[int, dict[str, object]]:
            return {1: {"status": "live", "last_frame_at": observed_at_ms}}

    async def exercise() -> dict[str, object]:
        settings = RelaySettings(
            relay_token=CONSOLE_KEY,
            log_dir=tmp_path,
            media_poll_interval_ms=10,
        )
        relay = RelayRuntime(settings, media_observer=Observer(), clock=lambda: 1_234)
        session = relay.session("media-observer-test")
        session.registry.apply_join(
            parse_membership_request(
                membership_payload(
                    action="join",
                    event_id="join-media-1",
                    session="media-observer-test",
                )
            )
        )
        await relay.start()
        try:
            await asyncio.sleep(0.03)
            return session.current_state()["drones"][0]["video"]
        finally:
            await relay.stop()

    assert asyncio.run(exercise()) == {"status": "live", "last_frame_at": 1_234}


def test_latency_trace_reports_observed_stage_durations() -> None:
    trace = runtime.LatencyTrace(source_started_at_ms=1_000)
    trace.mark_path_ready(1_130)
    trace.mark_hls_frame_decoded(1_360)

    assert trace.report(stream="drone1") == {
        "v": 1,
        "stream": "drone1",
        "source_started_at": 1_000,
        "path_ready_at": 1_130,
        "hls_frame_decoded_at": 1_360,
        "publish_to_path_ready_ms": 130,
        "publish_to_hls_frame_ms": 360,
    }


def test_latency_trace_rejects_regressive_observations() -> None:
    trace = runtime.LatencyTrace(source_started_at_ms=1_000)

    with pytest.raises(ValueError, match="before source start"):
        trace.mark_path_ready(999)


def test_frame_latency_report_uses_source_and_render_timestamps() -> None:
    samples = [
        {"source_timestamp_ms": 1_000, "rendered_timestamp_ms": 1_120},
        {"source_timestamp_ms": 2_000, "rendered_timestamp_ms": 2_180},
        {"source_timestamp_ms": 3_000, "rendered_timestamp_ms": 3_310},
    ]

    assert runtime.summarize_frame_latency(samples, protocol="whep") == {
        "v": 1,
        "protocol": "whep",
        "sample_count": 3,
        "latency_ms": [120, 180, 310],
        "p50_ms": 180,
        "p95_ms": 310,
        "max_ms": 310,
        "target_ms": 300,
        "target_met": False,
    }


def test_hls_frame_latency_is_reported_without_inventing_a_threshold() -> None:
    report = runtime.summarize_frame_latency(
        [{"source_timestamp_ms": 1_000, "rendered_timestamp_ms": 2_050}],
        protocol="hls",
    )

    assert report["p95_ms"] == 1_050
    assert report["target_ms"] is None
    assert report["target_met"] is None


def test_webcam_acceptance_template_starts_pending_without_false_evidence() -> None:
    artifact = runtime.webcam_acceptance_template(created_at_ms=1_756_700_000_000)

    assert artifact["status"] == "pending"
    assert artifact["reason"] == "awaiting_webcam_run"
    assert artifact["whep"]["target_ms"] == 300
    assert artifact["whep"]["samples"] == []
    assert artifact["hls"]["target_ms"] is None
    assert artifact["recording"] is None


def test_console_origin_configures_both_browser_media_transports() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "MTX_WEBRTCALLOWORIGINS: ${SWEEP_CONSOLE_ORIGIN" in compose
    assert "MTX_HLSALLOWORIGINS: ${SWEEP_CONSOLE_ORIGIN" in compose
