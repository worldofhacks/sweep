"""An isolated local fleet: production relay and signed fake-node command sockets.

    uv run python -m adapters.sim.demo --count 4 --console-dist console/dist

No .env file is loaded. The bound address is always loopback, and fresh fleet
credentials are generated for this process. The optional built console
receives them through its existing same-origin bootstrap endpoint. Session logs
are retained after shutdown so the demo can be inspected and replayed.
Each fake node publishes telemetry at 5 Hz (20 fleet frames/s for four nodes).
The production 10 Hz state fan-out and one-second freshness gates remain unchanged.
"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import signal
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from adapters.dji_mini3.fake_node import FakeNode, FakeNodeConfig
from adapters.sim.navigation_demo import navigation_demo_runtime
from adapters.sim.search_demo import search_demo
from arbiter.safety import SafetyConfig
from planner.models import Geofence
from planner.navigation_deployment import NavigationDeployment
from planner.planner import PlanningConfig
from relay.autonomy import AutonomyComposition, AutonomyConfig, create_autonomy_app
from relay.settings import AdapterBackend, RelaySettings
from relay.voice import AudioUpload, TranscriptionError, TranscriptService
from relay.voice_telemetry import NoOpVoiceTraceSink

_WAIT_S = 10.0


class _UnavailableDemoTranscription:
    def transcribe(self, upload: AudioUpload) -> str:
        raise TranscriptionError("the isolated demo has no configured transcription provider")


@dataclass(frozen=True, slots=True)
class DemoConfig:
    count: int = 4
    port: int = 0
    session: str = field(default_factory=lambda: f"fleet-demo-{uuid.uuid4().hex[:12]}")
    log_dir: Path | None = None
    console_dist: Path | None = None
    console_origins: tuple[str, ...] = ()
    navigation_demo: bool = False
    search_demo: bool = False

    def __post_init__(self) -> None:
        if type(self.count) is not int or not 1 <= self.count <= 4:
            raise ValueError("demo count must be an integer from 1 through 4")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("demo port must be an integer from 0 through 65535")
        if (
            not self.session
            or len(self.session) > 100
            or any(
                not (character.isascii() and (character.isalnum() or character in "_-"))
                for character in self.session
            )
        ):
            raise ValueError("demo session must use 1 to 100 ASCII letters, digits, _ or -")
        if self.console_dist is not None and not (self.console_dist / "index.html").is_file():
            raise ValueError("console_dist must contain a built console index.html")


def demo_autonomy_config(*, navigation_demo: bool = False) -> AutonomyConfig:
    """Explicit kinematic-demo values; these are not hardware calibration."""
    return AutonomyConfig(
        planning=PlanningConfig(
            takeoff_altitude_m=1.0,
            translation_step_m=0.5,
            flight_speed_m_s=0.5,
            capture_yaw_speed_deg_s=30.0,
            capture_yaw_tolerance_deg=1.0,
            capture_pose_tolerance_m=0.1,
            capture_min_overlap_deg=10.0,
            capture_gimbal_pitch_deg=0.0,
            reconstruct_headings_deg=tuple(float(value) for value in range(0, 360, 45)),
            altitude_step_m=0.5,
            altitude_floor_z_m=0.0,
            altitude_configuration_id="isolated-demo-floor-v1",
            altitude_completion_tolerance_m=0.05,
        ),
        safety=SafetyConfig(
            geofence=Geofence(-10.0, 10.0, -10.0, 10.0, 0.0, 5.0),
            ceiling_m=4.0,
            min_spacing_m=0.8,
            battery_reserve_fraction=0.2,
            battery_critical_fraction=0.1,
            battery_cost_per_m=0.01,
            min_link_quality=0.4,
            max_link_age_ms=1_000,
            min_position_quality=0.5,
            max_position_age_ms=1_000,
            operator_timeout_ms=10_000,
            max_future_clock_skew_ms=1_000,
            min_capture_storage_bytes=1_000_000,
            max_capture_pose_drift_m=0.2,
            max_capture_gimbal_error_deg=1.0,
            positioning_loss_hold_ms=3_000,
            motion_conflict_window_ms=500,
        ),
        navigation_deployment=(
            NavigationDeployment(
                navigation_demo_runtime(),
                4,
                "isolated-demo-control",
                "synthetic",
                "demo-navigation",
            )
            if navigation_demo
            else None
        ),
    )


class FleetDemo:
    """Own one relay listener and its fake nodes; never connect to an existing relay.

    ``autonomy_config`` lets an integration harness compose an explicit language
    runtime using the same app factory, without loading deployment credentials.
    """

    def __init__(
        self,
        config: DemoConfig | None = None,
        *,
        autonomy_config: AutonomyConfig | None = None,
        configure_app: Callable[[FastAPI, AutonomyComposition], None] | None = None,
    ) -> None:
        self.config = config or DemoConfig()
        self.autonomy_config = autonomy_config or demo_autonomy_config(
            navigation_demo=self.config.navigation_demo or self.config.search_demo
        )
        self.search = search_demo(self.autonomy_config) if self.config.search_demo else None
        if self.search is not None:
            self.autonomy_config = self.search.config
        self._frames_stopped = threading.Event()
        self._frames_thread: threading.Thread | None = None
        self._configure_app = configure_app
        self.log_dir = self.config.log_dir or Path(tempfile.mkdtemp(prefix="sweep-fleet-demo-"))
        self.token = secrets.token_urlsafe(32)
        self._keys = {
            drone_id: secrets.token_urlsafe(32) for drone_id in range(1, self.config.count + 1)
        }
        self.nodes: dict[int, FakeNode] = {}
        self._node_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError("a fleet demo may only be started once")
        self._started = True
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener = listener
            listener.bind(("127.0.0.1", self.config.port))
            self.port = listener.getsockname()[1]
            self.http_url = f"http://127.0.0.1:{self.port}"
            self.ws_url = f"ws://127.0.0.1:{self.port}"
            settings = RelaySettings(
                relay_token=self.token.encode(),
                adapter_keys={drone_id: key.encode() for drone_id, key in self._keys.items()},
                adapter_backend=AdapterBackend.REMOTE,
                log_dir=self.log_dir,
                console_origins=(self.http_url, *self.config.console_origins),
            )
            self.app, self.composition = create_autonomy_app(
                settings,
                self.autonomy_config,
                transcript_service_factory=lambda _: TranscriptService(
                    transcription=_UnavailableDemoTranscription(), tracer=NoOpVoiceTraceSink()
                ),
                detection_stream_factory=None
                if self.search is None
                else self.search.stream_factory,
                detection_detector_factory=None
                if self.search is None
                else self.search.detector_factory,
                detection_pose_provider_factory=None
                if self.search is None
                else self.search.pose_provider_factory,
                detection_camera_provider_factory=None
                if self.search is None
                else self.search.camera_provider_factory,
            )
            self._add_routes()
            if self._configure_app is not None:
                self._configure_app(self.app, self.composition)
            self.server = uvicorn.Server(
                uvicorn.Config(
                    self.app,
                    log_level="warning",
                    lifespan="on",
                    timeout_graceful_shutdown=2,
                    # Profiling showed compression dominating the local feed's event-loop work.
                    ws_per_message_deflate=False,
                )
            )
            self._thread = threading.Thread(
                target=self.server.run,
                kwargs={"sockets": [listener]},
                name="fleet-demo-relay",
                daemon=True,
            )
            self._thread.start()
            _wait_until(lambda: self.server.started, "relay startup")
            self.runtime = self.app.state.relay_runtime
            for drone_id in self._keys:
                node = self._new_node(drone_id)
                self.nodes[drone_id] = node
                node.start()
            _wait_until(
                lambda: (
                    len(self.drones()) == self.config.count
                    and all(drone["membership"] == "ready" for drone in self.drones().values())
                ),
                "all demo aircraft ready",
            )
            if self.search is not None:
                self._frames_thread = threading.Thread(target=self._publish_frames, daemon=True)
                self._frames_thread.start()
        except BaseException:
            self.stop()
            raise

    def _new_node(self, drone_id: int) -> FakeNode:
        return FakeNode(
            FakeNodeConfig(
                relay_url=self.ws_url,
                session=self.config.session,
                drone_id=drone_id,
                token=self._keys[drone_id],
                adapter_id=f"isolated-demo-node-{drone_id}",
                home=self._home(drone_id),
                telemetry_hz=5.0,
            )
        )

    def _home(self, drone_id: int) -> tuple[float, float, float]:
        if self.config.navigation_demo or self.config.search_demo:
            return ((0.5, 1.5, 0.0), (0.5, 3.5, 0.0), (3.5, 3.5, 0.0), (5.5, 3.5, 0.0))[
                drone_id - 1
            ]
        return (float((drone_id - 1) * 2), 0.0, 0.0)

    def drones(self) -> dict[int, dict[str, object]]:
        session = self.runtime.sessions.get(self.config.session)
        if session is None:
            return {}
        return {drone["drone_id"]: drone for drone in session.current_state()["drones"]}

    def bootstrap(self) -> dict[str, object]:
        return {
            "relay": {"baseUrl": self.ws_url, "sessionId": self.config.session, "token": self.token}
        }

    def status(self) -> dict[str, object]:
        return {
            "kind": "isolated_fake_node_demo",
            "session": self.config.session,
            "count": self.config.count,
            "navigation_demo": self.config.navigation_demo,
            "console_url": self.http_url if self.config.console_dist is not None else None,
            "relay_url": self.ws_url,
            "replay_url": f"{self.http_url}/session/{self.config.session}",
            "log_dir": str(self.log_dir.resolve()),
            "drones": list(self.drones().values()),
        }

    def disconnect_node(self, drone_id: int) -> None:
        with self._node_lock:
            node = self.nodes.get(drone_id)
            if node is None:
                raise ValueError("unknown demo aircraft")
            if self.drones()[drone_id]["membership"] == "disconnected":
                raise ValueError("demo aircraft is already disconnected")
            node.stop()
            _wait_until(
                lambda: self.drones()[drone_id]["membership"] == "disconnected",
                "demo node disconnection",
            )

    def rejoin_node(self, drone_id: int) -> None:
        """Join a fresh landed fixture at its original home with a new epoch."""
        with self._node_lock:
            if drone_id not in self.nodes:
                raise ValueError("unknown demo aircraft")
            if self.drones()[drone_id]["membership"] != "disconnected":
                raise ValueError("disconnect the demo aircraft before rejoining")
            node = self._new_node(drone_id)
            self.nodes[drone_id] = node
            node.start()
            _wait_until(
                lambda: self.drones()[drone_id]["membership"] == "ready", "demo node rejoin"
            )

    def _add_routes(self) -> None:
        @self.app.get("/relay-bootstrap.json")
        def bootstrap() -> JSONResponse:
            return JSONResponse(self.bootstrap(), headers={"Cache-Control": "no-store"})

        @self.app.get("/demo/status")
        def status() -> dict[str, object]:
            return self.status()

        @self.app.post("/demo/nodes/{drone_id}/{action}")
        def node_action(
            drone_id: int, action: str, authorization: str | None = Header(default=None)
        ) -> dict[str, object]:
            if not hmac.compare_digest(authorization or "", f"Bearer {self.token}"):
                raise HTTPException(status_code=401, detail="demo authentication required")
            operation = {"disconnect": self.disconnect_node, "rejoin": self.rejoin_node}.get(action)
            if operation is None:
                raise HTTPException(status_code=404, detail="unknown demo action")
            try:
                operation(drone_id)
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from None
            return self.status()

        if self.config.console_dist is not None:
            self.app.mount("/", StaticFiles(directory=self.config.console_dist, html=True))

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._frames_stopped.set()
        if self._frames_thread is not None:
            self._frames_thread.join(timeout=_WAIT_S)
        with self._node_lock:
            for node in self.nodes.values():
                node.stop()
        if self._thread is not None:
            self.server.should_exit = True
            self._thread.join(timeout=_WAIT_S)
            if self._thread.is_alive():
                self.server.force_exit = True
                self._thread.join(timeout=_WAIT_S)
        if hasattr(self, "composition"):
            self.composition.close()
        if self._listener is not None:
            self._listener.close()

    def _publish_frames(self) -> None:
        assert self.search is not None
        while not self._frames_stopped.wait(0.04):
            self.search.publish_frame()


def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError(f"timed out waiting for {description}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--session")
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--console-dist", type=Path)
    parser.add_argument("--console-origin", action="append", default=[])
    parser.add_argument("--navigation-demo", action="store_true")
    parser.add_argument("--search-demo", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = DemoConfig(
            count=args.count,
            port=args.port,
            session=args.session or f"fleet-demo-{uuid.uuid4().hex[:12]}",
            log_dir=args.log_dir,
            console_dist=args.console_dist,
            console_origins=tuple(args.console_origin),
            navigation_demo=args.navigation_demo,
            search_demo=args.search_demo,
        )
    except ValueError as error:
        parser.error(str(error))
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    with FleetDemo(config) as demo:
        print(json.dumps({"type": "demo.ready", **demo.status()}), flush=True)
        stopped.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
