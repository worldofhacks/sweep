from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import uvicorn
from websockets.asyncio.client import connect

from perception.ohmni_pts_capture import CapturedFrame
from relay.app import create_app
from relay.auth import sign_event
from relay.contracts import NodeType
from relay.observation_ingress import ObservationConfiguration
from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    SourceBinding,
    decode_observation,
)
from relay.settings import AdapterBackend, RelaySettings
from tools.ohmni_live_tag_mapper import (
    AcceptedObservationArchive,
    ArchiveConfig,
    LiveScope,
    LiveTagMapper,
    MapperConfig,
    publish_observations,
)

SESSION = "mapper-relay-pipeline"
GROUND_ID = 12
GROUND_KEY = b"ground-adapter-key-for-mapper-pipeline"
LOCALIZATION_KEY = b"localization-key-for-mapper-pipeline"
CAMERA_SOURCE = "ohmni-live-camera"
TAG_SOURCE = "ohmni-live-tag"
CLOCK_ID = "robot-monotonic"
CLOCK_MAPPING_ID = "mapper-pipeline-clock"
CAPTURE_NS = 10_000_000_000
WAIT_S = 5.0
type Event = dict[str, object]
type PipelineResult = tuple[int, Event, tuple[Event, ...]]


@dataclass(slots=True)
class _RelayServer:
    port: int
    server: uvicorn.Server

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"


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
            }
        ]


def _configuration(relay_reference_ms: int) -> ObservationConfiguration:
    pose_scope = (SESSION, GROUND_ID, 1, "ohmni-pose")
    camera_scope = (SESSION, GROUND_ID, 1, CAMERA_SOURCE)
    tag_scope = (SESSION, GROUND_ID, 1, TAG_SOURCE)
    mapping = ClockMapping(
        CLOCK_MAPPING_ID,
        CLOCK_ID,
        "ns",
        CAPTURE_NS,
        relay_reference_ms,
        1,
        1_000_000,
        10,
    )
    return ObservationConfiguration(
        bindings=(
            SourceBinding(
                *pose_scope,
                "ground",
                ("odom", "body"),
                ("pose",),
                producer_role="adapter",
            ),
            SourceBinding(
                *camera_scope,
                "ground",
                ("camera",),
                ("camera_frame",),
                allowed_clock_mapping_ids=(CLOCK_MAPPING_ID,),
                producer_role="localization",
            ),
            SourceBinding(
                *tag_scope,
                "ground",
                ("camera", "tag:7"),
                ("tag_observation",),
                allowed_clock_mapping_ids=(CLOCK_MAPPING_ID,),
                producer_role="localization",
            ),
        ),
        frames=FrameRegistry(
            (
                FrameDeclaration("odom", "odom", "right_handed_z_up", "m", *pose_scope),
                FrameDeclaration("body", "body", "forward_left_up", "m", *pose_scope),
                FrameDeclaration("camera", "camera", "right_down_forward", "m", *camera_scope),
                FrameDeclaration("camera", "camera", "right_down_forward", "m", *tag_scope),
                FrameDeclaration("tag:7", "tag", "right_up_outward", "m", *tag_scope),
            )
        ),
        clock_mappings=(mapping,),
    )


def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {description}")


@pytest.fixture
def relay_server(tmp_path: Path) -> Iterator[_RelayServer]:
    settings = RelaySettings(
        relay_token=b"console-key-for-mapper-relay-pipeline",
        adapter_keys={GROUND_ID: GROUND_KEY},
        localization_keys={GROUND_ID: LOCALIZATION_KEY},
        node_types={GROUND_ID: NodeType.GROUND},
        log_dir=tmp_path,
        adapter_backend=AdapterBackend.REMOTE,
        observation_configuration=_configuration(time.time_ns() // 1_000_000),
    )
    app = create_app(settings)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=2)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    _wait_until(lambda: server.started, "relay startup")
    try:
        yield _RelayServer(listener.getsockname()[1], server)
    finally:
        server.should_exit = True
        thread.join(timeout=WAIT_S)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=WAIT_S)


async def _receive_until(socket, predicate: Callable[[Event], bool]) -> Event:
    received = []
    for _ in range(32):
        raw = json.loads(await asyncio.wait_for(socket.recv(), WAIT_S))
        received.append(raw)
        if isinstance(raw, dict) and predicate(raw):
            return raw
    raise AssertionError(f"relay did not emit the expected frame: {received!r}")


async def _authenticate(socket, source: str, token: bytes, *, receive_state: bool = True) -> None:
    await socket.send(
        json.dumps(
            {
                "v": 1,
                "type": "auth",
                "source": source,
                "drone_id": GROUND_ID,
                "token": token.decode(),
            }
        )
    )
    if receive_state:
        accepted = json.loads(await asyncio.wait_for(socket.recv(), WAIT_S))
        assert accepted["type"] == "auth.accepted"
        state = json.loads(await asyncio.wait_for(socket.recv(), WAIT_S))
        assert state["type"] == "state"


async def _join_ground(socket) -> None:
    unsigned = {
        "v": 1,
        "t": time.time_ns() // 1_000_000,
        "type": "membership",
        "event_id": "ground-join",
        "session": SESSION,
        "drone_id": GROUND_ID,
        "action": "join",
        "adapter_id": "mapper-pipeline-ground",
        "capabilities": ["ground_drive"],
        "node_type": "ground",
    }
    await socket.send(json.dumps({**unsigned, "signature": sign_event(unsigned, GROUND_KEY)}))
    await _receive_until(
        socket,
        lambda event: event.get("type") == "membership" and event.get("membership") == "registered",
    )
    pose = {
        "v": 1,
        "type": "observation",
        "event_id": "ground-pose",
        "session": SESSION,
        "device_id": GROUND_ID,
        "connection_epoch": 1,
        "source_id": "ohmni-pose",
        "node_type": "ground",
        "frame": "odom",
        "confidence": 0.8,
        "t_capture": None,
        "t_source_receipt": {"clock_id": "ground-monotonic", "unit": "ns", "value": 1},
        "clock_mapping_id": None,
        "payload": {
            "kind": "pose",
            "pose": {
                "parent_frame": "odom",
                "child_frame": "body",
                "x_m": 0.0,
                "y_m": 0.0,
                "z_m": 0.0,
                "qx": 0.0,
                "qy": 0.0,
                "qz": 0.0,
                "qw": 1.0,
            },
        },
    }
    await socket.send(json.dumps(pose))
    ready_unsigned = {
        "v": 1,
        "t": time.time_ns() // 1_000_000,
        "type": "membership",
        "event_id": "ground-ready",
        "session": SESSION,
        "drone_id": GROUND_ID,
        "action": "readiness",
        "connection_epoch": 1,
        "drive_authority": True,
        "safety_operator_present": True,
        "local_stop_ready": True,
        "heartbeat_ready": True,
        "pose_identity": {
            "event_id": "ground-pose",
            "session": SESSION,
            "connection_epoch": 1,
            "source_id": "ohmni-pose",
            "frame": "odom",
        },
    }
    await socket.send(
        json.dumps({**ready_unsigned, "signature": sign_event(ready_unsigned, GROUND_KEY)})
    )
    state = await _receive_until(
        socket,
        lambda event: (
            event.get("type") == "state"
            and any(
                drone.get("drone_id") == GROUND_ID and drone.get("membership") == "ready"
                for drone in event.get("drones", [])
                if isinstance(drone, dict)
            )
        ),
    )
    assert state["session"] == SESSION


def _mapper(*, event_prefix: str, receipt_ns: int) -> LiveTagMapper:
    return LiveTagMapper(
        MapperConfig(
            session=SESSION,
            device_id=GROUND_ID,
            camera_source_id=CAMERA_SOURCE,
            tag_source_id=TAG_SOURCE,
            camera_frame="camera",
            camera_serial="ohmni-head-12",
            calibration_id="sha256:" + "a" * 64,
            clock_id=CLOCK_ID,
            clock_mapping_id=CLOCK_MAPPING_ID,
            maximum_capture_lag_ns=5_000_000,
            confidence=0.8,
            covariance_m2=(0.01, 0.0, 0.0, 0.0, 0.01, 0.0, 0.0, 0.0, 0.02),
        ),
        _Detector(),  # type: ignore[arg-type]
        receipt_time_ns=iter((receipt_ns, receipt_ns + 1)).__next__,
        event_ids=iter((f"{event_prefix}-camera", f"{event_prefix}-tag")).__next__,
    )


def _frame() -> CapturedFrame:
    return CapturedFrame(np.zeros((12, 16, 3), dtype=np.uint8), CAPTURE_NS)


async def _run_pipeline(url: str, archive_output: Path) -> PipelineResult:
    scope = LiveScope(SESSION, GROUND_ID, 1)
    mapper = _mapper(event_prefix="accepted", receipt_ns=CAPTURE_NS + 1_000_000)
    archive = AcceptedObservationArchive(
        archive_output,
        scope=scope,
        mapper=mapper.config,
        config=ArchiveConfig("ohmni-pose", "ohmni-lidar", "odom", "body", "lidar"),
    )
    async with connect(f"{url}/ws/{SESSION}") as adapter:
        await _authenticate(adapter, "adapter", GROUND_KEY)
        await _join_ground(adapter)
        async with connect(f"{url}/ws/{SESSION}") as localizer:
            await _authenticate(localizer, "localization", LOCALIZATION_KEY, receive_state=False)
            published = await publish_observations(
                localizer,
                mapper,
                (_frame(),),
                receive_timeout_s=WAIT_S,
                archive=archive,
            )
            rejected = _mapper(event_prefix="rejected", receipt_ns=CAPTURE_NS + 2_000_000)
            wrong_role = rejected.observations(scope, _frame())[0].to_mapping()
            await adapter.send(json.dumps(wrong_role))
            role_refusal = await _receive_until(
                adapter,
                lambda event: (
                    event.get("type") == "refusal" and event.get("reason") == "source_role_mismatch"
                ),
            )
            wrong_epoch = {**wrong_role, "event_id": "wrong-epoch", "connection_epoch": 2}
            await localizer.send(json.dumps(wrong_epoch))
            epoch_refusal = await _receive_until(
                localizer,
                lambda event: (
                    event.get("type") == "refusal"
                    and event.get("reason") == "stale_connection_epoch"
                ),
            )
    manifest = archive.finish()
    observations = tuple(
        decode_observation(line).to_mapping()
        for line in (archive_output / "observations.jsonl").read_bytes().splitlines()
    )
    return published, manifest, (*observations, role_refusal, epoch_refusal)


def test_mapper_publishes_accepted_observations_into_the_real_relay_archive(
    relay_server: _RelayServer, tmp_path: Path
) -> None:
    published, manifest, events = asyncio.run(_run_pipeline(relay_server.url, tmp_path / "archive"))

    accepted = events[:2]
    role_refusal, epoch_refusal = events[2:]
    assert published == 2
    assert {event["payload"]["kind"] for event in accepted} == {
        "camera_frame",
        "tag_observation",
    }
    assert {event["source_id"] for event in accepted} == {CAMERA_SOURCE, TAG_SOURCE}
    assert all(
        event["session"] == SESSION
        and event["device_id"] == GROUND_ID
        and event["connection_epoch"] == 1
        and event["node_type"] == "ground"
        and event["clock_mapping_id"] == CLOCK_MAPPING_ID
        and event["t_capture"] == {"clock_id": CLOCK_ID, "unit": "ns", "value": CAPTURE_NS}
        and event["t_source_receipt"]["value"] >= CAPTURE_NS
        and isinstance(event["t_ingest"], int)
        for event in accepted
    )
    assert manifest["observations"]["count"] == 2
    assert manifest["observations"]["kinds"] == {
        "camera_frame": 1,
        "tag_observation": 1,
        "pose": 0,
        "range_scan": 0,
    }
    assert role_refusal["reason"] == "source_role_mismatch"
    assert epoch_refusal["reason"] == "stale_connection_epoch"
