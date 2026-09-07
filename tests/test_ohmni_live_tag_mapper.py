from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from perception.ohmni_clock_probe import parse_probe_reply, probe_clock
from perception.ohmni_pts_capture import CapturedFrame
from relay.observation_ingress import ObservationConfiguration, ObservationIngress
from relay.observations import ClockMapping, FrameDeclaration, FrameRegistry, SourceBinding
from tools.ohmni_live_tag_mapper import (
    LiveMapperError,
    LiveScope,
    LiveTagMapper,
    MapperConfig,
    publish_observations,
    scope_from_state,
)


class _Detector:
    camera_serial = "ohmni-head-12"
    width = 16
    height = 12

    def detect(self, _image: np.ndarray) -> list[dict[str, object]]:
        return [
            {
                "tag_id": 7,
                "pose_accepted": True,
                "T_camera_tag": np.array(
                    [
                        [1.0, 0.0, 0.0, 1.0],
                        [0.0, 1.0, 0.0, 2.0],
                        [0.0, 0.0, 1.0, 3.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ]
                ),
                "reason": "pose",
                "size_m": 0.16,
                "corners_px": [[1.0, 1.0], [4.0, 1.0], [4.0, 4.0], [1.0, 4.0]],
                "pixel_frame": "rectified_camera",
                "reprojection_rms_px": 0.2,
            },
            {
                "tag_id": 8,
                "pose_accepted": True,
                "T_camera_tag": np.array(
                    [
                        [1.0, 0.0, 0.0, 4.0],
                        [0.0, 1.0, 0.0, 5.0],
                        [0.0, 0.0, 1.0, 6.0],
                        [0.0, 0.0, 0.0, 1.0],
                    ]
                ),
                "reason": "pose",
                "size_m": 0.16,
                "corners_px": [[6.0, 1.0], [9.0, 1.0], [9.0, 4.0], [6.0, 4.0]],
                "pixel_frame": "rectified_camera",
                "reprojection_rms_px": 0.2,
            },
        ]


def _mapper(*, receipt_ns: int = 1_100_000_000) -> LiveTagMapper:
    return LiveTagMapper(
        MapperConfig(
            session="live-12",
            device_id=12,
            camera_source_id="ohmni-live-camera",
            tag_source_id="ohmni-live-tag",
            camera_frame="camera",
            camera_serial="ohmni-head-12",
            calibration_id="sha256:" + "a" * 64,
            clock_id="ohmni12-boot-monotonic",
            clock_mapping_id="ohmni12-live",
            maximum_capture_lag_ns=5_000_000_000,
            confidence=0.8,
            covariance_m2=(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
        ),
        _Detector(),  # type: ignore[arg-type]
        receipt_time_ns=lambda: receipt_ns,
        event_ids=iter(("camera-event", "tag-seven-event", "tag-eight-event")).__next__,
    )


def _frame() -> CapturedFrame:
    return CapturedFrame(np.zeros((12, 16, 3), dtype=np.uint8), 1_000_000_000)


def test_mapper_emits_capture_timestamped_camera_and_tag_events_accepted_by_live_ingress() -> None:
    mapper = _mapper()
    scope = LiveScope("live-12", 12, 9)
    camera, *tags = mapper.observations(scope, _frame())

    assert camera.t_capture is not None
    assert camera.t_capture.value == 1_000_000_000
    assert [tag.t_capture for tag in tags] == [camera.t_capture, camera.t_capture]
    assert [tag.payload["tag_id"] for tag in tags] == [7, 8]
    assert all(tag.source_id == "ohmni-live-tag" for tag in tags)
    assert all(tag.payload["tag_pose"] is not None for tag in tags)

    mapping = ClockMapping(
        "ohmni12-live",
        "ohmni12-boot-monotonic",
        "ns",
        1_000_000_000,
        1_000,
        1,
        1_000_000,
        0,
    )
    camera_scope = ("live-12", 12, 9, "ohmni-live-camera")
    tag_scope = ("live-12", 12, 9, "ohmni-live-tag")
    ingress = ObservationIngress(
        ObservationConfiguration(
            bindings=(
                SourceBinding(
                    *camera_scope,
                    "ground",
                    ("camera",),
                    ("camera_frame",),
                    allowed_clock_mapping_ids=(mapping.mapping_id,),
                    producer_role="localization",
                ),
                SourceBinding(
                    *tag_scope,
                    "ground",
                    ("camera", "tag:7", "tag:8"),
                    ("tag_observation",),
                    allowed_clock_mapping_ids=(mapping.mapping_id,),
                    producer_role="localization",
                ),
            ),
            frames=FrameRegistry(
                (
                    FrameDeclaration("camera", "camera", "right_down_forward", "m", *camera_scope),
                    FrameDeclaration("camera", "camera", "right_down_forward", "m", *tag_scope),
                    FrameDeclaration("tag:7", "tag", "right_up_outward", "m", *tag_scope),
                    FrameDeclaration("tag:8", "tag", "right_up_outward", "m", *tag_scope),
                )
            ),
            clock_mappings=(mapping,),
        ),
        "live-12",
        0,
    )

    admitted_camera = ingress.accept(camera, now=1_100, producer_role="localization")
    admitted_tags = [
        ingress.accept(tag, now=1_100 + index * 10, producer_role="localization")
        for index, tag in enumerate(tags)
    ]

    assert admitted_camera.submission.payload["kind"] == "camera_frame"
    assert [item.submission.payload["tag_id"] for item in admitted_tags] == [7, 8]


def test_mapper_rejects_a_receipt_that_precedes_the_actual_v4l2_pts() -> None:
    with pytest.raises(LiveMapperError, match="precedes"):
        _mapper(receipt_ns=999_999_999).observations(LiveScope("live-12", 12, 9), _frame())


@pytest.mark.parametrize("membership", ("registered", "ready", "degraded"))
def test_scope_uses_current_active_ground_epoch_from_relay_state(membership: str) -> None:
    scope = scope_from_state(
        {
            "type": "state",
            "session": "live-12",
            "drones": [
                {
                    "drone_id": 12,
                    "node_type": "ground",
                    "membership": membership,
                    "connection_epoch": 17,
                }
            ],
        },
        session="live-12",
        device_id=12,
    )

    assert scope == LiveScope("live-12", 12, 17)


def test_publisher_derives_scope_before_sending_canonical_events() -> None:
    class Socket:
        def __init__(self) -> None:
            self.inbound = iter(
                (
                    json.dumps({"type": "auth.accepted"}),
                    json.dumps(
                        {
                            "type": "state",
                            "session": "live-12",
                            "drones": [
                                {
                                    "drone_id": 12,
                                    "node_type": "ground",
                                    "membership": "ready",
                                    "connection_epoch": 17,
                                }
                            ],
                        }
                    ),
                    json.dumps({"type": "observation", "event_id": "camera:camera-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-seven-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-eight-event"}),
                )
            )
            self.sent: list[dict[str, object]] = []

        async def recv(self) -> str:
            return next(self.inbound)

        async def send(self, message: str) -> None:
            self.sent.append(json.loads(message))

    relay = Socket()
    count = asyncio.run(publish_observations(relay, _mapper(), (_frame(),)))

    assert count == 3
    assert {item["source_id"] for item in relay.sent} == {"ohmni-live-camera", "ohmni-live-tag"}
    assert {item["connection_epoch"] for item in relay.sent} == {17}


def test_publisher_spaces_multiple_tag_events_from_one_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import ohmni_live_tag_mapper

    class Socket:
        def __init__(self) -> None:
            self.inbound = iter(
                (
                    json.dumps({"type": "auth.accepted"}),
                    json.dumps(
                        {
                            "type": "state",
                            "session": "live-12",
                            "drones": [
                                {
                                    "drone_id": 12,
                                    "node_type": "ground",
                                    "membership": "ready",
                                    "connection_epoch": 17,
                                }
                            ],
                        }
                    ),
                    json.dumps({"type": "observation", "event_id": "camera:camera-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-seven-event"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-eight-event"}),
                )
            )

        async def recv(self) -> str:
            return next(self.inbound)

        async def send(self, _message: str) -> None:
            return None

    delays: list[float] = []

    async def wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(ohmni_live_tag_mapper.asyncio, "sleep", wait)
    assert (
        asyncio.run(
            publish_observations(Socket(), _mapper(), (_frame(),), tag_submit_interval_ms=10)
        )
        == 3
    )
    assert len(delays) == 1
    assert 0 < delays[0] <= 0.01


def test_publisher_spaces_tag_events_across_consecutive_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import ohmni_live_tag_mapper

    class SingleTagDetector(_Detector):
        def detect(self, image: np.ndarray) -> list[dict[str, object]]:
            return super().detect(image)[:1]

    mapper = LiveTagMapper(
        _mapper().config,
        SingleTagDetector(),
        receipt_time_ns=lambda: 1_100_000_000,
        event_ids=iter(("camera-one", "tag-one", "camera-two", "tag-two")).__next__,
    )

    class Socket:
        def __init__(self) -> None:
            self.inbound = iter(
                (
                    json.dumps({"type": "auth.accepted"}),
                    json.dumps(
                        {
                            "type": "state",
                            "session": "live-12",
                            "drones": [
                                {
                                    "drone_id": 12,
                                    "node_type": "ground",
                                    "membership": "ready",
                                    "connection_epoch": 17,
                                }
                            ],
                        }
                    ),
                    json.dumps({"type": "observation", "event_id": "camera:camera-one"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-one"}),
                    json.dumps({"type": "observation", "event_id": "camera:camera-two"}),
                    json.dumps({"type": "observation", "event_id": "tag:tag-two"}),
                )
            )

        async def recv(self) -> str:
            return next(self.inbound)

        async def send(self, _message: str) -> None:
            return None

    delays: list[float] = []

    async def wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(ohmni_live_tag_mapper.asyncio, "sleep", wait)
    assert (
        asyncio.run(
            publish_observations(Socket(), mapper, (_frame(), _frame()), tag_submit_interval_ms=10)
        )
        == 4
    )
    assert len(delays) == 1
    assert 0 < delays[0] <= 0.01


def test_relay_state_chatter_cannot_extend_submission_confirmation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import ohmni_live_tag_mapper
    from tools.ohmni_live_tag_mapper import _confirm_submission

    state = json.dumps(
        {
            "type": "state",
            "session": "live-12",
            "drones": [
                {
                    "drone_id": 12,
                    "node_type": "ground",
                    "membership": "ready",
                    "connection_epoch": 9,
                }
            ],
        }
    )

    class Chatter:
        async def recv(self) -> str:
            await asyncio.sleep(0)
            return state

    event = _mapper().observations(LiveScope("live-12", 12, 9), _frame())[0]
    monkeypatch.setattr(ohmni_live_tag_mapper, "_receive_timeout", lambda _value: 0.001)
    with pytest.raises(LiveMapperError, match="timed out waiting"):
        asyncio.run(_confirm_submission(Chatter(), LiveScope("live-12", 12, 9), event, 1))


def test_silent_relay_times_out_during_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import ohmni_live_tag_mapper
    from tools.ohmni_live_tag_mapper import _authenticated_scope

    class Silent:
        async def recv(self) -> str:
            await asyncio.Future()
            raise AssertionError("unreachable")

    monkeypatch.setattr(ohmni_live_tag_mapper, "_receive_timeout", lambda _value: 0.001)
    with pytest.raises(LiveMapperError, match="timed out waiting"):
        asyncio.run(_authenticated_scope(Silent(), _mapper(), 1))


def test_main_closes_the_sidecar_and_removes_the_reverse_tunnel_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tools import ohmni_live_tag_mapper

    class Detector:
        camera_serial = "ohmni-live"
        width = 16
        height = 12

    class Source:
        closed = False

        def makefile(self, _mode: str):
            raise RuntimeError("sidecar stream failed")

        def close(self) -> None:
            self.closed = True

    source = Source()
    calls: list[tuple[str, int]] = []
    args = SimpleNamespace(
        calibration="unused",
        calibration_sha256="a" * 64,
        camera_serial="ohmni-live",
        tag_sizes={7: 0.16},
        session="live-12",
        device_id=12,
        camera_source_id="ohmni-live-camera",
        tag_source_id="ohmni-live-tag",
        camera_frame="camera",
        clock_id="robot-boot",
        clock_mapping_id="robot-live",
        maximum_capture_lag_ms=1_000,
        confidence=0.8,
        covariance=(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
        adb="adb",
        adb_serial="robot:5555",
        pts_port=18555,
        sidecar_connect_timeout_s=5,
        tag_submit_interval_ms=10,
        relay_receive_timeout_s=5,
    )
    monkeypatch.setattr(ohmni_live_tag_mapper, "read_calibration", lambda *_args: {})
    monkeypatch.setattr(
        ohmni_live_tag_mapper, "CameraTagDetector", lambda *_args, **_kwargs: Detector()
    )
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_qualify_clock",
        lambda _args: SimpleNamespace(robot_time_ns=lambda value: value),
    )
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_adb_reverse",
        lambda _adb, _serial, port: calls.append(("setup", port)),
    )
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_remove_adb_reverse",
        lambda _adb, _serial, port: calls.append(("remove", port)),
    )

    async def serve(_port: int, _timeout_s: float) -> Source:
        return source

    monkeypatch.setattr(ohmni_live_tag_mapper, "_serve_one", serve)
    with pytest.raises(RuntimeError, match="sidecar stream failed"):
        asyncio.run(ohmni_live_tag_mapper._main_async(args))
    assert source.closed is True
    assert calls == [("setup", 18555), ("remove", 18555)]


def test_clock_qualification_requires_the_pinned_robot_boot_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tools import ohmni_live_tag_mapper

    ticks = iter((1_000_000_000, 1_002_000_000))
    monkeypatch.setattr(ohmni_live_tag_mapper.time, "monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(
        ohmni_live_tag_mapper,
        "_adb_clock_query",
        lambda _adb, _serial: "12345678-1234-1234-1234-123456789abc\n39.82\n",
    )
    args = SimpleNamespace(
        adb="adb",
        adb_serial="10.10.0.74:5555",
        clock_probes=1,
        maximum_clock_error_ms=10,
        boot_id="12345678-1234-1234-1234-123456789abc",
    )

    mapping = ohmni_live_tag_mapper._qualify_clock(args)

    assert mapping.boot_id == args.boot_id


def test_mapper_sets_up_and_removes_the_robot_to_host_sidecar_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from tools import ohmni_live_tag_mapper

    calls: list[list[str]] = []
    monkeypatch.setattr(
        ohmni_live_tag_mapper.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )

    ohmni_live_tag_mapper._adb_reverse("adb", "robot:5555", 18555)
    ohmni_live_tag_mapper._remove_adb_reverse("adb", "robot:5555", 18555)

    assert calls == [
        ["adb", "-s", "robot:5555", "reverse", "tcp:18555", "tcp:18555"],
        ["adb", "-s", "robot:5555", "reverse", "--remove", "tcp:18555"],
    ]


def test_mapper_refuses_to_wait_indefinitely_for_the_robot_sidecar() -> None:
    from tools.ohmni_live_tag_mapper import _serve_one

    with pytest.raises(LiveMapperError, match="timed out waiting"):
        asyncio.run(_serve_one(0, 0.001))


def test_adb_clock_query_executes_the_robot_monotonic_protocol_through_adb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools import ohmni_live_tag_mapper

    loader = tmp_path / "loader"
    loader.write_text('#!/bin/sh\nexec "$@"\n')
    loader.chmod(0o755)
    monkeypatch.setattr(ohmni_live_tag_mapper, "OHMNI_LOADER", str(loader))
    monkeypatch.setattr(ohmni_live_tag_mapper, "OHMNI_PYTHON", sys.executable)
    local_run = subprocess.run
    replies: list[str] = []

    def adb_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:4] == ["adb", "-s", "robot:5555", "shell"]
        assert kwargs["timeout"] == ohmni_live_tag_mapper.ADB_CLOCK_QUERY_TIMEOUT_S
        completed = local_run(["sh", "-c", command[4]], check=False, capture_output=True, text=True)
        replies.append(completed.stdout)
        return completed

    monkeypatch.setattr(ohmni_live_tag_mapper.subprocess, "run", adb_run)
    sample = probe_clock(
        lambda: ohmni_live_tag_mapper._adb_clock_query("adb", "robot:5555"),
        monotonic_ns=lambda: time.clock_gettime_ns(time.CLOCK_MONOTONIC),
    )
    boot_id, timestamp_ns, resolution_ns = parse_probe_reply(replies[0])

    assert boot_id == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert timestamp_ns == sample.robot_monotonic_ns
    assert resolution_ns == 1
    assert sample.host_before_ns <= sample.robot_monotonic_ns <= sample.host_after_ns


@pytest.mark.parametrize("membership", ("leaving", "disconnected", "unknown"))
def test_scope_rejects_a_noncurrent_ground_epoch(membership: str) -> None:
    state = {
        "type": "state",
        "session": "live-12",
        "drones": [
            {
                "drone_id": 12,
                "node_type": "ground",
                "membership": membership,
                "connection_epoch": 17,
            }
        ],
    }

    with pytest.raises(LiveMapperError, match="no current active epoch"):
        scope_from_state(state, session="live-12", device_id=12)


def test_sidecar_socket_handoff_preserves_bytes_sent_at_connect() -> None:
    from tools.ohmni_live_tag_mapper import _serve_one

    payload = b"nut-sidecar-preamble"

    async def receive() -> bytes:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        accepting = asyncio.create_task(_serve_one(port, 1))
        await asyncio.sleep(0)
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        source = await accepting
        try:
            assert source.getblocking() is True
            return source.recv(len(payload))
        finally:
            source.close()
            writer.close()
            await writer.wait_closed()

    assert asyncio.run(receive()) == payload
