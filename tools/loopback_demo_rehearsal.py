"""Run a loopback-only console rehearsal against the real relay and protocol clients.

The rehearsal starts one FakeNode and one signed localization publisher.  Its
navigation deployment is the verified flight fixture used by the integration
suite, re-signed inside a temporary directory for the new session and clock. It
never reads a physical fleet configuration or binds either operator port.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import secrets
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

import uvicorn
from websockets.asyncio.client import connect

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from planner.models import Geofence
from planner.navigation import (
    ArrivalSlot,
    ArtifactPin,
    NavigationArtifact,
    NavigationPermission,
    Pose,
)
from planner.navigation_deployment import NavigationDeployment, load_navigation_deployment
from planner.navigation_runtime import navigation_configuration_digest
from planner.test_navigation_runtime import KEY as APPROVAL_KEY
from relay.auth import sign_event
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.control_frames import sign_localization_frame
from relay.control_localization import (
    ControlLocalizationPins,
    ControlLocalizationProjector,
    ControlLocalizationWire,
)
from relay.map_authoring import MapAuthoringStore
from relay.navigation_wire import wire_config_digest_candidates
from relay.settings import AdapterBackend, RelaySettings
from relay.tests.test_platform_navigation_execution import _deployment
from tests.autonomy_fixtures import planning_config, safety_config
from tests.world_bundle_fixtures import fixture_world_draft
from tools.loopback_search_fixture import (
    SyntheticFrameStream,
    synthetic_detector,
    synthetic_lobby_search_configuration,
)
from tools.map_geometry import generate
from tools.map_validate import content_hash

RELAY_HOST = "127.0.0.1"
SESSION_PREFIX = "loopback-rehearsal"
DEFAULT_LIFETIME_MS = 60 * 60 * 1_000
STARTUP_TIMEOUT_S = 15.0


class RehearsalError(RuntimeError):
    pass


def epoch_ms() -> int:
    return time.time_ns() // 1_000_000


def unused_loopback_port(*, excluded: set[int] | None = None) -> int:
    blocked = excluded or set()
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((RELAY_HOST, 0))
            port = int(listener.getsockname()[1])
        if port not in blocked and port not in {5173, 8010}:
            return port


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _arrival_slots() -> tuple[ArrivalSlot, ...]:
    return (
        ArrivalSlot("demo-west-slot", "demo-west", Pose(-0.8, 0.0, 1.0, "level_1"), 0.05, 0.05),
        ArrivalSlot("lobby-slot", "lobby", Pose(0.0, 0.0, 1.0, "level_1"), 0.05, 0.05),
        ArrivalSlot("demo-east-slot", "demo-east", Pose(0.8, 0.0, 1.0, "level_1"), 0.05, 0.05),
    )


def _synthetic_zone(zone_id: str, polygon: list[list[float]]) -> dict[str, object]:
    return {
        "id": zone_id,
        "floor_id": "level_1",
        "polygon": polygon,
        "z_min_m": 0.0,
        "z_max_m": 3.0,
    }


def _three_destination_deployment(directory: Path) -> NavigationDeployment:
    base = _deployment(directory)
    document = json.loads(base.path.read_text())
    world = directory / document["bundle_directory"]
    zones_path = world / "zones.yaml"
    zones = json.loads(zones_path.read_text())
    zones["zones"] = [
        _synthetic_zone(
            "demo-west", [[-1.4, -0.8], [-0.3, -0.8], [-0.3, 0.8], [-1.4, 0.8], [-1.4, -0.8]]
        ),
        _synthetic_zone(
            "lobby", [[-0.25, -0.8], [0.25, -0.8], [0.25, 0.8], [-0.25, 0.8], [-0.25, -0.8]]
        ),
        _synthetic_zone(
            "demo-east", [[0.3, -0.8], [1.4, -0.8], [1.4, 0.8], [0.3, 0.8], [0.3, -0.8]]
        ),
    ]
    _write_json(zones_path, zones)
    manifest_path = world / "manifest.yaml"
    manifest = json.loads(manifest_path.read_text())
    for name in ("tags.yaml", "zones.yaml", "obstacles.yaml"):
        manifest["files"][name] = sha256((world / name).read_bytes()).hexdigest()
    manifest["content_sha256"] = content_hash(manifest)
    _write_json(manifest_path, manifest)
    accepted = {manifest["bundle_version"]: manifest["content_sha256"]}

    authoring = directory / document["geometry_authoring"]
    authoring_document = json.loads(authoring.read_text())
    authoring_document["bundle_content_sha256"] = manifest["content_sha256"]
    _write_json(authoring, authoring_document)
    geometry = directory / document["geometry_directory"]
    for child in geometry.iterdir():
        child.unlink()
    geometry.rmdir()
    generate(world, authoring, geometry, accepted)
    report_path = geometry / "geometry.json"
    report = json.loads(report_path.read_text())
    geometry_sha256 = sha256(report_path.read_bytes()).hexdigest()

    world_path = directory / document["world_localization_file"]
    localization = json.loads(world_path.read_text())
    localization["accepted_versions"] = accepted
    localization["publisher"]["drones"][0]["fuser"]["geometry_id"] = report["authoring_sha256"]
    pins = localization["devices"][0]["pins"]
    pins["map_content_sha256"] = manifest["content_sha256"]
    pins["geometry_id"] = report["authoring_sha256"]
    pins["geometry_sha256"] = geometry_sha256
    _write_json(world_path, localization)

    slots = _arrival_slots()
    permission = NavigationPermission(frozenset(slot.zone_id for slot in slots))
    frame = base.config.frames[0]
    control_pins = frame.control_pins
    if control_pins is None:
        raise RehearsalError("the flight fixture has no control-localization pins")
    frames = (
        replace(frame, control_pins=replace(control_pins, geometry_id=report["authoring_sha256"])),
    )
    profiles = {
        drone_id: replace(
            profile,
            map_sha256=manifest["content_sha256"],
            geometry_sha256=geometry_sha256,
        )
        for drone_id, profile in base.wire_profiles.items()
    }
    config = replace(
        base.config,
        frames=frames,
        wire_config_sha256=next(iter(wire_config_digest_candidates(profiles))),
    )
    artifact = NavigationArtifact.from_geometry_directory(
        world, geometry, accepted, slots, authoring=authoring
    )
    artifact = replace(
        artifact,
        zones=tuple(
            replace(zone, owner_approved=zone.zone_id in permission.permitted_zone_ids)
            for zone in artifact.zones
        ),
    )
    document["accepted_map_versions"] = accepted
    document["arrival_slots"] = [asdict(slot) for slot in slots]
    document["permission_zone_ids"] = sorted(permission.permitted_zone_ids)
    document["home_zone_id"] = "lobby"
    document["execution"] = asdict(config)
    document["wire_profiles"] = {
        str(drone_id): asdict(profile) for drone_id, profile in profiles.items()
    }
    _write_json(base.path, document)
    approval_path = directory / document["approval_file"]
    approval = json.loads(approval_path.read_text())
    approval["configuration_sha256"] = navigation_configuration_digest(
        artifact, config, permission, "lobby"
    )
    unsigned = {key: value for key, value in approval.items() if key != "signature"}
    approval["signature"] = sign_event(unsigned, APPROVAL_KEY)
    _write_json(approval_path, approval)
    return load_navigation_deployment(base.path)


def _catalog_draft(deployment: NavigationDeployment) -> dict[str, object]:
    draft = fixture_world_draft()
    draft["metadata"]["mapVersion"] = deployment.artifact().map_pin.version
    draft["metadata"]["floorId"] = "level_1"
    zone = next(feature for feature in draft["features"] if feature["kind"] == "zone")
    zones = []
    for zone_id, name, points in (
        ("demo-west", "Demo West", [(1, 2), (3, 2), (3, 4), (1, 4), (1, 2)]),
        ("lobby", "Lobby", [(4, 2), (6, 2), (6, 4), (4, 4), (4, 2)]),
        ("demo-east", "Demo East", [(7, 2), (9, 2), (9, 4), (7, 4), (7, 2)]),
    ):
        item = copy.deepcopy(zone)
        item.update(id=zone_id, name=name, aliases=[], points=[{"x": x, "y": y} for x, y in points])
        zones.append(item)
    index = draft["features"].index(zone)
    draft["features"][index : index + 1] = zones
    return draft


def _rehearsal_deployment(
    directory: Path, *, session_id: str, now_ms: int, lifetime_ms: int
) -> tuple[NavigationDeployment, ControlLocalizationProjector, ControlLocalizationPins]:
    base = _three_destination_deployment(directory)
    artifact = base.artifact()
    document = json.loads(base.path.read_text())
    tuning_path = directory / document["wire_navigation_files"]["1"]
    tuning = json.loads(tuning_path.read_text())
    tuning["limits"]["tracking_timeout_ms"] = 12_000
    encoded_tuning = json.dumps(tuning, sort_keys=True, separators=(",", ":")).encode()
    tuning_path.write_bytes(encoded_tuning)
    tuning_digest = sha256(encoded_tuning).hexdigest()
    profile_document = document["wire_profiles"]["1"]
    profile_document.update(
        max_authorization_lifetime_ms=12_000,
        tracking_timeout_ms=12_000,
        navigation_config_sha256=tuning_digest,
    )
    frame = base.config.frames[0]
    original_pins = frame.control_pins
    if original_pins is None:
        raise RehearsalError("the flight fixture has no control-localization pins")
    mapping = replace(
        original_pins.clock_mapping,
        capture_reference_s=now_ms / 1_000,
        relay_reference_ms=now_ms,
    )
    pins = replace(original_pins, clock_mapping=mapping)
    frames = (replace(frame, control_pins=pins),)
    profiles = {
        drone_id: replace(
            profile,
            clock_lease_expires_at_ms=now_ms + lifetime_ms,
            max_authorization_lifetime_ms=12_000,
            tracking_timeout_ms=12_000,
            navigation_config_sha256=tuning_digest,
        )
        for drone_id, profile in base.wire_profiles.items()
    }
    config = replace(
        base.config,
        frames=frames,
        segment_timeout_ms=12_000,
        wire_config_sha256=next(iter(wire_config_digest_candidates(profiles))),
    )
    world_path = directory / "world-localization.json"
    world = json.loads(world_path.read_text())
    world["publisher"]["session"] = session_id
    publisher_drone = world["publisher"]["drones"][0]
    publisher_clock = publisher_drone["clock_mapping"]
    publisher_clock.update(capture_reference_s=now_ms / 1_000, relay_reference_ms=now_ms)
    publisher_drone["live_capture_clock"]["capture_reference_s"] = now_ms / 1_000
    world_path.write_text(json.dumps(world))
    document["execution"] = asdict(config)
    document["wire_profiles"] = {
        str(drone_id): asdict(profile) for drone_id, profile in profiles.items()
    }
    base.path.write_text(json.dumps(document))
    approval_path = directory / "approval.json"
    approval = json.loads(approval_path.read_text())
    approval.update(
        session=session_id,
        issued_at_ms=now_ms - 1_000,
        expires_at_ms=now_ms + lifetime_ms,
        configuration_sha256=navigation_configuration_digest(
            artifact, config, base.permission, base.home_zone_id
        ),
    )
    unsigned = {key: value for key, value in approval.items() if key != "signature"}
    approval["signature"] = sign_event(unsigned, APPROVAL_KEY)
    approval_path.write_text(json.dumps(approval))
    deployment = load_navigation_deployment(base.path)
    pins = deployment.config.frames[0].control_pins
    if pins is None:
        raise RehearsalError("the rehearsed flight fixture has no control-localization pins")
    projector = ControlLocalizationProjector(
        {1: pins},
        relay_clock_id=mapping.relay_clock_id,
        max_clock_error_ms=2,
        max_fix_age_ms=500,
        max_velocity_age_ms=200,
        max_height_age_ms=200,
        max_position_uncertainty_p95_m=0.3,
    )
    return deployment, projector, pins


def _seed_catalog(
    maps: MapAuthoringStore, session_id: str, deployment: NavigationDeployment
) -> tuple[dict[str, str], ArtifactPin]:
    draft = _catalog_draft(deployment)
    reference = maps.save(session_id, draft, None, "loopback-rehearsal")
    validation = maps.validate(session_id, reference, "loopback-rehearsal")
    if not validation["valid"]:
        raise RehearsalError("the synthetic rehearsal map did not validate")
    maps.approve(session_id, reference, validation["validationId"], "loopback-rehearsal")
    version = draft["metadata"]["mapVersion"]
    if not isinstance(version, str):
        raise RehearsalError("the synthetic rehearsal map has no map version")
    return reference, ArtifactPin(version, reference["contentHash"])


def _bind_authoring_map(
    deployment: NavigationDeployment, authoring_map_pin: ArtifactPin
) -> NavigationDeployment:
    artifact = deployment.artifact()
    config = replace(deployment.config, authoring_map_pin=authoring_map_pin)
    document = json.loads(deployment.path.read_text())
    document["execution"] = asdict(config)
    _write_json(deployment.path, document)
    approval_path = deployment.path.parent / document["approval_file"]
    approval = json.loads(approval_path.read_text())
    approval["configuration_sha256"] = navigation_configuration_digest(
        artifact, config, deployment.permission, deployment.home_zone_id
    )
    unsigned = {key: value for key, value in approval.items() if key != "signature"}
    approval["signature"] = sign_event(unsigned, APPROVAL_KEY)
    _write_json(approval_path, approval)
    return load_navigation_deployment(deployment.path)


def _select_catalog(app: Any, session_id: str, reference: dict[str, str]) -> None:
    platform = app.state.platform_services
    platform.navigation.select_map(session_id, {"reference": reference}, "loopback-rehearsal")


class MovingControlPosePublisher:
    def __init__(
        self,
        *,
        relay_url: str,
        session_id: str,
        token: bytes,
        pins: ControlLocalizationPins,
        position: Callable[[], tuple[float, float, float] | None],
    ) -> None:
        self.relay_url = relay_url
        self.session_id = session_id
        self.token = token
        self.pins = pins
        self.position = position
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RehearsalError("the localization publisher is already running")

        def runner() -> None:
            try:
                asyncio.run(self._run())
            except BaseException as error:
                self._failure = error
            finally:
                self._ready.set()

        self._thread = threading.Thread(target=runner, name="loopback-localization", daemon=True)
        self._thread.start()
        if not self._ready.wait(STARTUP_TIMEOUT_S):
            raise RehearsalError("the localization publisher did not authenticate in time")
        if self._failure is not None:
            raise RehearsalError("the localization publisher failed to start") from self._failure

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(STARTUP_TIMEOUT_S)

    async def _run(self) -> None:
        async with connect(f"{self.relay_url}/ws/{self.session_id}") as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "v": 1,
                        "type": "auth",
                        "source": "localization",
                        "drone_id": 1,
                        "token": self.token.decode(),
                    }
                )
            )
            accepted = json.loads(await websocket.recv())
            if accepted.get("type") != "auth.accepted":
                raise RehearsalError("the relay refused the localization publisher")
            await websocket.recv()
            self._ready.set()
            sequence = 0
            reader = asyncio.create_task(self._drain(websocket))
            try:
                while not self._stop.is_set():
                    if reader.done():
                        reader.result()
                    sequence += 1
                    current_s = time.time()
                    position = self.position()
                    if position is None:
                        await asyncio.sleep(0.05)
                        continue
                    x_m, y_m, z_m = position
                    wire = ControlLocalizationWire(
                    drone_id=1,
                    connection_epoch=1,
                    map_id=self.pins.map_id,
                    geometry_id=self.pins.geometry_id,
                    camera_calibration_id=self.pins.camera_calibration_id,
                    body_extrinsics_id=self.pins.body_extrinsics_id,
                    capture_clock_id=self.pins.clock_mapping.capture_clock_id,
                    evaluated_at_s=current_s,
                    position_map_enu_m=(x_m, y_m, z_m),
                    covariance_map_enu_m2=(
                        (0.00000001, 0.0, 0.0),
                        (0.0, 0.00000001, 0.0),
                        (0.0, 0.0, 0.00000001),
                    ),
                    fix_age_s=0.0,
                    velocity_age_s=0.0,
                    height_age_s=0.0,
                    confidence="green",
                    loss_age_s=None,
                    status="ready",
                    control_eligible=True,
                    flight_approved=False,
                    reason="loopback_rehearsal",
                    source_ids=self.pins.source_ids,
                    clock_mapping=self.pins.clock_mapping,
                )
                    frame = sign_localization_frame(
                        wire,
                        timestamp_ms=epoch_ms(),
                        event_id=f"loopback-pose-{sequence}",
                        session=self.session_id,
                        signing_key=self.token,
                    )
                    await websocket.send(json.dumps(frame))
                    await asyncio.sleep(0.02)
            finally:
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader

    @staticmethod
    async def _drain(websocket: Any) -> None:
        while True:
            await websocket.recv()


class LoopbackDemoRehearsal:
    """Own every temporary process and credential created for a rehearsal."""

    def __init__(
        self,
        *,
        relay_port: int | None = None,
        console_port: int | None = None,
        start_console: bool = False,
        lifetime_ms: int = DEFAULT_LIFETIME_MS,
        bootstrap_path: Path | None = None,
    ) -> None:
        excluded = {port for port in (relay_port, console_port) if port is not None}
        self.relay_port = relay_port or unused_loopback_port(excluded=excluded)
        self.console_port = console_port or unused_loopback_port(excluded={self.relay_port})
        if {self.relay_port, self.console_port} & {
            5173,
            8010,
        } or self.relay_port == self.console_port:
            raise ValueError("the rehearsal ports must be distinct and cannot be 5173 or 8010")
        if start_console:
            raise ValueError("start the console with its isolated browser-test Vite configuration")
        self.start_console = start_console
        self.lifetime_ms = lifetime_ms
        self.session_id = f"{SESSION_PREFIX}-{secrets.token_hex(6)}"
        self.relay_url = f"ws://{RELAY_HOST}:{self.relay_port}"
        self.console_url = f"http://{RELAY_HOST}:{self.console_port}/"
        self._temporary = tempfile.TemporaryDirectory(prefix="sweep-loopback-rehearsal-")
        self.directory = Path(self._temporary.name)
        self.bootstrap_path = bootstrap_path or self.directory / "console-bootstrap.json"
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._app: Any | None = None
        self._node: FakeNode | None = None
        self._pose_publisher: MovingControlPosePublisher | None = None
        self._composition: Any = None
        self._started = False

    def __enter__(self) -> LoopbackDemoRehearsal:
        self.start()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._started:
            return
        now = epoch_ms()
        relay_token = secrets.token_urlsafe(48).encode()
        adapter_token = secrets.token_urlsafe(48).encode()
        localization_token = secrets.token_urlsafe(48).encode()
        settings = RelaySettings(
            relay_token=relay_token,
            adapter_keys={1: adapter_token},
            localization_keys={1: localization_token},
            log_dir=self.directory / "logs",
            adapter_backend=AdapterBackend.REMOTE,
            console_origins=(self.console_url.removesuffix("/"),),
        )
        deployment, projector, pins = _rehearsal_deployment(
            self.directory / "navigation",
            session_id=self.session_id,
            now_ms=now,
            lifetime_ms=self.lifetime_ms,
        )
        maps = MapAuthoringStore(settings.log_dir / "platform" / "maps.sqlite3", clock_ms=epoch_ms)
        reference, authoring_map_pin = _seed_catalog(maps, self.session_id, deployment)
        deployment = _bind_authoring_map(deployment, authoring_map_pin)
        self._write_bootstrap(relay_token, adapter_token, localization_token)
        search, search_detection = synthetic_lobby_search_configuration(deployment)
        config = AutonomyConfig(
            planning=replace(planning_config(), flight_speed_m_s=0.2),
            safety=replace(
                safety_config(),
                geofence=Geofence(-100.0, 100.0, -100.0, 100.0, -100.0, 100.0),
                ceiling_m=50.0,
            ),
            control_localization_projector=projector,
            navigation=deployment,
            search=search,
            search_detection=search_detection,
        )
        app, self._composition = create_autonomy_app(
            settings,
            config,
            clock=epoch_ms,
            detection_stream_factory=SyntheticFrameStream,
            detection_detector_factory=synthetic_detector,
        )
        self._app = app
        self._start_relay(app)
        self._composition.session(self.session_id)
        _select_catalog(app, self.session_id, reference)
        self._node = FakeNode(
            FakeNodeConfig(
                relay_url=self.relay_url,
                session=self.session_id,
                drone_id=1,
                token=adapter_token.decode(),
                adapter_id="loopback-fake-node-1",
                home=(-20.0, 9.8, -29.0),
                telemetry_hz=50,
                capabilities=(
                    "flight",
                    "navigate",
                    "pano_360",
                    "reconstruct_8",
                    "single_still",
                    "test:synthetic",
                ),
            )
        )
        self._node._aircraft.state = "hovering"
        self._node.start()
        self._wait_for_ready_node(app)
        self._composition.runtime.sessions[self.session_id].update_control_projection(
            selection=(1,), armed=True
        )
        self._pose_publisher = MovingControlPosePublisher(
            relay_url=self.relay_url,
            session_id=self.session_id,
            token=localization_token,
            pins=pins,
            position=self._node_position,
        )
        self._pose_publisher.start()
        self._started = True

    def _node_position(self) -> tuple[float, float, float] | None:
        state = self._composition.runtime.sessions[self.session_id].current_state()
        drone = next(
            (item for item in state["drones"] if item.get("drone_id") == 1),
            None,
        )
        telemetry = None if drone is None else drone.get("telemetry")
        if not isinstance(telemetry, dict) or any(
            not isinstance(telemetry.get(axis), (int, float)) for axis in ("x", "y", "z")
        ):
            return None
        return float(telemetry["x"]), float(telemetry["y"]), float(telemetry["z"])

    def stop(self) -> None:
        if self._pose_publisher is not None:
            self._pose_publisher.stop()
            self._pose_publisher = None
        if self._node is not None:
            self._node.stop()
            self._node = None
        if self._server is not None:
            self._server.should_exit = True
        if self._server_thread is not None:
            self._server_thread.join(STARTUP_TIMEOUT_S)
            self._server_thread = None
        self._server = None
        self._app = None
        if self._composition is not None:
            self._composition.close()
            self._composition = None
        self.bootstrap_path.unlink(missing_ok=True)
        self._started = False
        self._temporary.cleanup()

    def _write_bootstrap(
        self, relay_token: bytes, adapter_token: bytes, localization_token: bytes
    ) -> None:
        os.chmod(self.directory, 0o700)
        payload = {
            "relay": {
                "origin": self.relay_url,
                "session": self.session_id,
                "token": relay_token.decode(),
            },
            "adapter": {"droneId": 1, "token": adapter_token.decode()},
            "localization": {"droneId": 1, "token": localization_token.decode()},
        }
        descriptor = os.open(self.bootstrap_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(payload, output)
        if self.bootstrap_path.stat().st_mode & 0o077:
            raise RehearsalError("the temporary credential bootstrap is not private")

    def _start_relay(self, app: Any) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=RELAY_HOST, port=self.relay_port, log_level="warning")
        )
        self._server_thread = threading.Thread(
            target=self._server.run, name="loopback-relay", daemon=True
        )
        self._server_thread.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while not self._server.started:
            if not self._server_thread.is_alive():
                raise RehearsalError("the relay stopped during startup")
            if time.monotonic() >= deadline:
                raise RehearsalError("the relay did not start in time")
            time.sleep(0.02)

    def _wait_for_ready_node(self, app: Any) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        latest: object = None
        while time.monotonic() < deadline:
            runtime = app.state.relay_runtime
            session = runtime.sessions.get(self.session_id)
            if session is not None:
                state = session.current_state()
                latest = state.get("drones")
                if state.get("drones") and state["drones"][0].get("membership") == "ready":
                    return
            time.sleep(0.05)
        node_failure = None if self._node is None else self._node._failure
        raise RehearsalError(f"the FakeNode did not become ready ({latest!r}; {node_failure!r})")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the loopback-only Sweep demo rehearsal.")
    parser.add_argument("--relay-port", type=int)
    parser.add_argument("--console-port", type=int)
    parser.add_argument("--bootstrap-file", type=Path)
    parser.add_argument("--duration", type=float, help="stop automatically after this many seconds")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    rehearsal = LoopbackDemoRehearsal(
        relay_port=args.relay_port,
        console_port=args.console_port,
        start_console=False,
        bootstrap_path=args.bootstrap_file,
    )
    stopping = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        rehearsal.start()
        print("SIMULATION ONLY — no physical aircraft or physical configuration is used.")
        print(f"Relay: {rehearsal.relay_url}")
        print(f"Bootstrap file: {rehearsal.bootstrap_path}")
        print("Temporary credentials are private and are removed when this rehearsal stops.")
        deadline = None if args.duration is None else time.monotonic() + args.duration
        while not stopping.wait(0.2):
            if deadline is not None and time.monotonic() >= deadline:
                break
    except (RehearsalError, ValueError) as error:
        print(f"rehearsal failed: {error}")
        return 1
    finally:
        rehearsal.stop()
        signal.signal(signal.SIGINT, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
