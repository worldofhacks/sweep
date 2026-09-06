"""Occupancy grids from lidar scans, their PNG encoding, and the session map endpoint."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from planner.models import DeviceClass, Geofence
from relay.app import create_app, default_session_mapper
from relay.auth import Principal
from relay.contracts import SensorFrame, SensorKind, SensorPose, parse_sensor
from relay.mapping import (
    FALLBACK_HALF_EXTENT_M,
    FREE_PIXEL,
    OCCUPIED_PIXEL,
    UNKNOWN_PIXEL,
    MapImage,
    OccupancyGrid,
    SessionMapper,
)
from relay.settings import RelaySettings
from relay.tests.conftest import (
    CONSOLE_KEY,
    GROUND_ID,
    GROUND_KEY,
    SESSION,
    EventIds,
    MutableClock,
    ground_membership_payload,
    sensor_payload,
)
from tests.autonomy_fixtures import planning_config, safety_config

BEARER = {"Authorization": f"Bearer {CONSOLE_KEY.decode()}"}
ROOM_HALF_M = 1.5


def _square_room_ranges(
    half_m: float = ROOM_HALF_M,
    *,
    x: float = 0.0,
    y: float = 0.0,
    yaw_deg: float = 0.0,
    count: int = 360,
) -> list[int]:
    """Ranges a lidar would report standing inside a square room centred on the origin.

    Every bearing hits the nearest wall, so the scan is a closed rectangle: an interior a
    ray must cross to reach the wall, and nothing at all about the floor outside it.
    """
    ranges: list[int] = []
    for index in range(count):
        bearing = math.radians(yaw_deg + index * 360.0 / count)
        dx, dy = math.cos(bearing), math.sin(bearing)
        nearest = math.inf
        for bound, delta, origin in (
            (half_m, dx, x),
            (-half_m, dx, x),
            (half_m, dy, y),
            (-half_m, dy, y),
        ):
            if abs(delta) < 1e-12:
                continue
            distance = (bound - origin) / delta
            if distance <= 0:
                continue
            hit_x, hit_y = x + distance * dx, y + distance * dy
            inside = (
                -half_m - 1e-9 <= hit_x <= half_m + 1e-9
                and -half_m - 1e-9 <= hit_y <= half_m + 1e-9
            )
            if inside:
                nearest = min(nearest, distance)
        ranges.append(round(nearest * 100))
    return ranges


def _frame(
    ranges_cm: list[int],
    *,
    x: float = 0.0,
    y: float = 0.0,
    yaw_deg: float = 0.0,
    range_max_m: float = 12.0,
    session: str = SESSION,
) -> SensorFrame:
    """A parsed sensor frame, so the tests exercise the same object the relay retains."""
    return parse_sensor(
        sensor_payload(
            event_id="scan-1",
            session=session,
            ranges_cm=ranges_cm,
            pose={"x": x, "y": y, "yaw_deg": yaw_deg},
            range_max_m=range_max_m,
        )
    )


def _grid(half_extent_m: float = 5.0) -> OccupancyGrid:
    return OccupancyGrid(
        min_x=-half_extent_m,
        min_y=-half_extent_m,
        max_x=half_extent_m,
        max_y=half_extent_m,
    )


def _decode(image: MapImage) -> np.ndarray:
    decoded = cv2.imdecode(np.frombuffer(image.png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded is not None
    return decoded


def test_a_square_room_scan_frees_the_interior_and_marks_the_walls() -> None:
    grid = _grid()
    grid.integrate(_frame(_square_room_ranges()), now=1_756_700_000_123)

    assert grid.value_at(0.0, 0.0) < 0
    for x, y in ((1.0, 0.0), (-1.0, 0.4), (0.0, -1.2), (0.9, 0.9)):
        assert grid.value_at(x, y) < 0, (x, y)
    for x, y in ((ROOM_HALF_M, 0.0), (-ROOM_HALF_M, 0.0), (0.0, ROOM_HALF_M), (0.0, -ROOM_HALF_M)):
        assert grid.value_at(x, y) > 0, (x, y)
    # The floor beyond the walls was never crossed by a ray, so it stays unknown.
    for x, y in ((2.5, 2.5), (0.0, 3.0), (-4.0, 0.0), (4.9, -4.9)):
        assert grid.value_at(x, y) == 0, (x, y)
    assert grid.updated_at == 1_756_700_000_123


def test_the_occupied_ring_is_the_room_and_nothing_wider() -> None:
    grid = _grid()
    grid.integrate(_frame(_square_room_ranges()), now=1)

    occupied = np.argwhere(grid.cells() > 0)
    rows, columns = occupied[:, 0], occupied[:, 1]
    lower, upper = (
        grid.cell_index(-ROOM_HALF_M, -ROOM_HALF_M),
        grid.cell_index(ROOM_HALF_M, ROOM_HALF_M),
    )
    # Every wall cell sits on the room's boundary, within one cell of rounding.
    assert rows.min() >= lower[0] - 1 and rows.max() <= upper[0] + 1
    assert columns.min() >= lower[1] - 1 and columns.max() <= upper[1] + 1
    # All four walls are represented, not just the ones the major axis favours.
    assert rows.min() <= lower[0] + 1 and rows.max() >= upper[0] - 1
    assert columns.min() <= lower[1] + 1 and columns.max() >= upper[1] - 1


def test_zero_ranges_are_skipped_rather_than_read_as_free_space() -> None:
    grid = _grid()
    grid.integrate(_frame([0] * 360), now=99)

    assert not grid.cells().any()
    assert grid.updated_at == 0

    # One bearing without a return leaves its own cells untouched while its neighbours
    # are cleared: a missing return is not evidence of an empty floor.
    ranges = _square_room_ranges()
    gap = list(ranges)
    for index in range(84, 97):
        gap[index] = 0
    with_gap = _grid()
    with_gap.integrate(_frame(gap), now=5)
    assert with_gap.value_at(0.0, ROOM_HALF_M) == 0
    assert with_gap.value_at(0.0, 1.0) == 0
    assert with_gap.value_at(ROOM_HALF_M, 0.0) > 0


def test_a_return_beyond_the_sensor_reach_clears_its_ray_without_a_hit() -> None:
    grid = _grid()
    ranges = [0] * 360
    ranges[0] = 800
    grid.integrate(_frame(ranges, range_max_m=4.0), now=1)

    assert grid.value_at(2.0, 0.0) < 0
    assert grid.value_at(3.9, 0.0) < 0
    # Nothing was seen inside the sensor's reach, so no cell on that bearing is occupied.
    assert not (grid.cells() > 0).any()


def test_a_return_closer_than_the_sensor_minimum_is_skipped() -> None:
    grid = _grid()
    ranges = [0] * 360
    ranges[0] = 5
    grid.integrate(_frame(ranges), now=1)

    assert not grid.cells().any()


def test_a_scan_from_outside_the_grid_changes_nothing() -> None:
    grid = _grid(half_extent_m=1.0)
    grid.integrate(_frame(_square_room_ranges(), x=40.0, y=40.0), now=7)

    assert not grid.cells().any()
    assert grid.updated_at == 0


def test_the_png_decodes_at_the_grid_size_with_row_zero_at_the_maximum_y() -> None:
    grid = _grid()
    grid.integrate(_frame(_square_room_ranges()), now=1_756_700_000_500)
    image = grid.to_png()
    decoded = _decode(image)

    assert decoded.shape == (grid.height, grid.width) == (200, 200)
    assert decoded.dtype == np.uint8
    assert set(np.unique(decoded).tolist()) == {OCCUPIED_PIXEL, UNKNOWN_PIXEL, FREE_PIXEL}
    assert image.width == 200 and image.height == 200
    assert image.updated_at == 1_756_700_000_500

    # Row 0 is the maximum y, so the +y wall is nearer the top of the image than the -y
    # wall, the opposite of their order in the stored array.
    top_row, column = grid.cell_index(0.0, ROOM_HALF_M)
    bottom_row, _ = grid.cell_index(0.0, -ROOM_HALF_M)
    assert decoded[grid.height - 1 - top_row, column] == OCCUPIED_PIXEL
    assert decoded[grid.height - 1 - bottom_row, column] == OCCUPIED_PIXEL
    assert grid.height - 1 - top_row < grid.height - 1 - bottom_row
    assert decoded[0].tolist() == [UNKNOWN_PIXEL] * grid.width


def test_the_headers_place_the_image_in_the_world() -> None:
    grid = OccupancyGrid(min_x=-2.0, min_y=-1.0, max_x=3.0, max_y=1.5)
    headers = grid.to_png().headers()

    assert headers["X-Sweep-Map-Resolution-M"] == "0.05"
    assert float(headers["X-Sweep-Map-Origin-X"]) == pytest.approx(-2.0)
    assert float(headers["X-Sweep-Map-Origin-Y"]) == pytest.approx(-1.0)
    assert headers["X-Sweep-Map-Width"] == "100"
    assert headers["X-Sweep-Map-Height"] == "50"
    assert headers["X-Sweep-Map-Updated-At"] == "0"


def test_reset_forgets_every_observation_and_keeps_the_extent() -> None:
    grid = _grid()
    grid.integrate(_frame(_square_room_ranges()), now=10)
    assert grid.cells().any()

    grid.reset(now=20)

    assert not grid.cells().any()
    assert grid.updated_at == 20
    assert (grid.width, grid.height, grid.origin_x) == (200, 200, -5.0)
    assert set(np.unique(_decode(grid.to_png())).tolist()) == {UNKNOWN_PIXEL}


def test_repeated_scans_accumulate_confidence_without_leaving_int8() -> None:
    grid = _grid()
    frame = _frame(_square_room_ranges())
    for _ in range(200):
        grid.integrate(frame, now=1)

    cells = grid.cells()
    assert cells.dtype == np.int8
    assert cells.max() <= 100 and cells.min() >= -100
    assert grid.value_at(ROOM_HALF_M, 0.0) > 0
    assert grid.value_at(0.0, 0.0) < 0


def test_a_grid_larger_than_the_cell_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        OccupancyGrid(min_x=-500.0, min_y=-500.0, max_x=500.0, max_y=500.0)


def test_the_extent_expands_the_geofence_and_falls_back_without_one() -> None:
    fenced = SessionMapper(geofence=Geofence(-3.0, 4.0, -2.0, 5.0, 0.0, 3.0))
    assert fenced.bounds == (-5.0, -4.0, 6.0, 7.0)

    unfenced = SessionMapper()
    assert unfenced.bounds == (
        -FALLBACK_HALF_EXTENT_M,
        -FALLBACK_HALF_EXTENT_M,
        FALLBACK_HALF_EXTENT_M,
        FALLBACK_HALF_EXTENT_M,
    )


def test_the_mapper_keeps_one_grid_per_session_and_resets_only_that_one() -> None:
    clock = MutableClock()
    mapper = SessionMapper(clock=clock)
    mapper.observe(_frame(_square_room_ranges(), session=SESSION))
    clock.advance(200)
    mapper.observe(_frame(_square_room_ranges(), session="session-other"))

    assert mapper.grid("session-unseen") is None
    assert mapper.render("session-unseen") is None
    assert mapper.reset("session-unseen") is False

    first, second = mapper.grid(SESSION), mapper.grid("session-other")
    assert first is not None and second is not None and first is not second
    assert first.updated_at == 1_756_700_000_000
    assert second.updated_at == 1_756_700_000_200

    clock.advance(300)
    assert mapper.reset(SESSION) is True
    assert not first.cells().any()
    assert second.cells().any()
    assert first.updated_at == 1_756_700_000_500


def test_the_default_mapper_takes_its_extent_from_the_safety_geofence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(Path("/tmp"))
    for name in ("SWEEP_SIM_CAMERA_JSON", "SWEEP_CONTROL_LOCALIZATION_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWEEP_PLANNING_JSON", json.dumps(asdict(planning_config())))
    monkeypatch.setenv(
        "SWEEP_SAFETY_JSON",
        json.dumps(
            asdict(replace(safety_config(), geofence=Geofence(-1.0, 2.0, -3.0, 4.0, 0.0, 3.0)))
        ),
    )
    mapper = default_session_mapper(settings, lambda: 0)
    assert mapper is not None
    assert mapper.bounds == (-3.0, -5.0, 4.0, 6.0)

    # Without the autonomy configuration the relay still serves a map, on the fixed extent.
    monkeypatch.delenv("SWEEP_SAFETY_JSON")
    fallback = default_session_mapper(settings, lambda: 0)
    assert fallback is not None
    assert fallback.bounds == (-10.0, -10.0, 10.0, 10.0)


def _settings(log_dir: Path) -> RelaySettings:
    return RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys={GROUND_ID: GROUND_KEY},
        device_classes={GROUND_ID: DeviceClass.GROUND_VEHICLE},
        log_dir=log_dir,
        intent_max_age_ms=5_000,
        transport_event_max_age_ms=5_000,
        future_clock_skew_ms=1_000,
        telemetry_freshness_ms=1_000,
    )


def _app(tmp_path: Path, clock: MutableClock, event_ids: EventIds, mapper: SessionMapper):
    return create_app(
        _settings(tmp_path),
        clock=clock,
        event_ids=event_ids,
        mapper_factory=lambda _settings, _clock: mapper,
    )


def _scan(session, clock: MutableClock, *, event_id: str = "scan-1", **overrides: object) -> None:
    """Feed one accepted scan through the same entry point the node socket uses."""
    principal = Principal(source="adapter", drone_id=GROUND_ID, signing_key=GROUND_KEY)
    events = session.process_frame(
        sensor_payload(event_id=event_id, timestamp=clock(), **overrides), principal
    )
    assert [event["type"] for event in events] == ["sensor"], events


def _join(session, clock: MutableClock) -> None:
    principal = Principal(source="adapter", drone_id=GROUND_ID, signing_key=GROUND_KEY)
    events = session.process_frame(
        ground_membership_payload(action="join", event_id="join-1", timestamp=clock()), principal
    )
    assert events[0]["type"] == "membership", events


def test_the_map_endpoint_refuses_a_request_without_the_relay_bearer(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    app = _app(tmp_path, clock, event_ids, SessionMapper(clock=clock))
    with TestClient(app) as client:
        session = app.state.relay_runtime.session(SESSION)
        _join(session, clock)
        _scan(session, clock)

        assert client.get(f"/api/sessions/{SESSION}/map").status_code == 401
        assert (
            client.get(
                f"/api/sessions/{SESSION}/map", headers={"Authorization": "Bearer wrong-token"}
            ).status_code
            == 401
        )
        assert client.post(f"/api/sessions/{SESSION}/map/reset").status_code == 401
        assert client.get(f"/api/sessions/{SESSION}/map", headers=BEARER).status_code == 200


def test_the_map_endpoint_is_404_until_the_session_has_a_scan(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    app = _app(tmp_path, clock, event_ids, SessionMapper(clock=clock))
    with TestClient(app) as client:
        session = app.state.relay_runtime.session(SESSION)
        assert client.get(f"/api/sessions/{SESSION}/map", headers=BEARER).status_code == 404

        _join(session, clock)
        _scan(session, clock)

        assert client.get(f"/api/sessions/{SESSION}/map", headers=BEARER).status_code == 200
        # A session nobody has opened has neither a grid nor a log to reset.
        assert client.get("/api/sessions/session-other/map", headers=BEARER).status_code == 404
        assert (
            client.post("/api/sessions/session-other/map/reset", headers=BEARER).status_code == 404
        )
        assert client.get("/api/sessions/../etc/map", headers=BEARER).status_code in {400, 404}


def test_the_map_endpoint_serves_the_grid_a_node_scan_built(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    mapper = SessionMapper(geofence=Geofence(-4.0, 4.0, -4.0, 4.0, 0.0, 3.0), clock=clock)
    app = _app(tmp_path, clock, event_ids, mapper)
    with TestClient(app) as client:
        session = app.state.relay_runtime.session(SESSION)
        _join(session, clock)
        _scan(
            session,
            clock,
            ranges_cm=_square_room_ranges(),
            pose={"x": 0.0, "y": 0.0, "yaw_deg": 0.0},
        )

        response = client.get(f"/api/sessions/{SESSION}/map", headers=BEARER)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["X-Sweep-Map-Resolution-M"] == "0.05"
    assert float(response.headers["X-Sweep-Map-Origin-X"]) == pytest.approx(-6.0)
    assert float(response.headers["X-Sweep-Map-Origin-Y"]) == pytest.approx(-6.0)
    assert response.headers["X-Sweep-Map-Width"] == "240"
    assert response.headers["X-Sweep-Map-Height"] == "240"
    assert response.headers["X-Sweep-Map-Updated-At"] == str(clock())

    decoded = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert decoded.shape == (240, 240)
    grid = mapper.grid(SESSION)
    assert grid is not None
    row, column = grid.cell_index(0.0, 0.0)
    assert decoded[grid.height - 1 - row, column] == FREE_PIXEL
    wall_row, wall_column = grid.cell_index(ROOM_HALF_M, 0.0)
    assert decoded[grid.height - 1 - wall_row, wall_column] == OCCUPIED_PIXEL


def test_the_reset_endpoint_clears_the_grid_and_is_audited(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    mapper = SessionMapper(clock=clock)
    app = _app(tmp_path, clock, event_ids, mapper)
    with TestClient(app) as client:
        session = app.state.relay_runtime.session(SESSION)
        _join(session, clock)
        _scan(session, clock)
        grid = mapper.grid(SESSION)
        assert grid is not None and grid.cells().any()

        clock.advance(1_000)
        response = client.post(f"/api/sessions/{SESSION}/map/reset", headers=BEARER)

        assert response.status_code == 200
        assert response.json()["type"] == "map_reset"
        assert response.json()["cleared"] is True
        assert not grid.cells().any()
        # The grid stays, so the endpoint keeps answering with an all-unknown image.
        after = client.get(f"/api/sessions/{SESSION}/map", headers=BEARER)
        assert after.status_code == 200
        assert after.headers["X-Sweep-Map-Updated-At"] == str(clock())
        decoded = cv2.imdecode(np.frombuffer(after.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        assert set(np.unique(decoded).tolist()) == {UNKNOWN_PIXEL}

        audited = [
            record["event"]
            for record in session.replay()["events"]
            if record["event"]["type"] == "map_reset"
        ]

    assert len(audited) == 1
    assert audited[0]["session"] == SESSION
    assert audited[0]["cleared"] is True
    assert audited[0]["t"] == clock()


def test_a_reset_before_the_first_scan_is_audited_as_nothing_cleared(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    app = _app(tmp_path, clock, event_ids, SessionMapper(clock=clock))
    with TestClient(app) as client:
        app.state.relay_runtime.session(SESSION)
        response = client.post(f"/api/sessions/{SESSION}/map/reset", headers=BEARER)

    assert response.status_code == 200
    assert response.json()["cleared"] is False


def test_a_node_scan_over_the_socket_reaches_the_map_endpoint(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    """The wiring end to end: an authenticated node's scan, a console's map request."""
    app = _app(tmp_path, clock, event_ids, SessionMapper(clock=clock))
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{SESSION}") as console:
            console.send_json(
                {"v": 1, "type": "auth", "source": "console", "token": CONSOLE_KEY.decode()}
            )
            assert console.receive_json()["type"] == "auth.accepted"
            with client.websocket_connect(f"/ws/{SESSION}") as node:
                node.send_json(
                    {
                        "v": 1,
                        "type": "auth",
                        "source": "adapter",
                        "drone_id": GROUND_ID,
                        "token": GROUND_KEY.decode(),
                    }
                )
                assert node.receive_json()["type"] == "auth.accepted"
                node.send_json(
                    ground_membership_payload(action="join", event_id="join-1", timestamp=clock())
                )
                node.send_json(
                    sensor_payload(
                        event_id="scan-1",
                        timestamp=clock(),
                        ranges_cm=_square_room_ranges(),
                        pose={"x": 0.0, "y": 0.0, "yaw_deg": 0.0},
                    )
                )
                for _ in range(40):
                    if console.receive_json()["type"] == "sensor":
                        break
                else:  # pragma: no cover - the fan-out is asserted in test_sensor
                    raise AssertionError("the scan was not fanned out to the console")

        response = client.get(f"/api/sessions/{SESSION}/map", headers=BEARER)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    decoded = cv2.imdecode(np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert OCCUPIED_PIXEL in decoded and FREE_PIXEL in decoded


def test_a_relay_without_a_mapper_reports_no_map_rather_than_failing(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds
) -> None:
    app = create_app(
        _settings(tmp_path),
        clock=clock,
        event_ids=event_ids,
        mapper_factory=lambda _settings, _clock: None,
    )
    with TestClient(app) as client:
        session = app.state.relay_runtime.session(SESSION)
        _join(session, clock)
        _scan(session, clock)

        assert client.get(f"/api/sessions/{SESSION}/map", headers=BEARER).status_code == 404
        reset = client.post(f"/api/sessions/{SESSION}/map/reset", headers=BEARER)

    assert reset.status_code == 200
    assert reset.json()["cleared"] is False


def test_the_grid_is_built_from_the_frame_the_relay_retained() -> None:
    """``SensorFrame`` is the only input; the grid never re-reads the wire payload."""
    frame = SensorFrame(
        1,
        1_756_700_000_000,
        "sensor",
        "scan-1",
        SESSION,
        GROUND_ID,
        1,
        SensorKind.LIDAR_SCAN,
        SensorPose(0.0, 0.0, 90.0),
        0.0,
        1.0,
        0.15,
        12.0,
        tuple([100] + [0] * 359),
    )
    grid = _grid()
    grid.integrate(frame, now=1)

    # Bearing 0 is the device's forward axis, and the pose's yaw points it along +y.
    assert grid.value_at(0.0, 1.0) > 0
    assert grid.value_at(1.0, 0.0) == 0
    assert grid.value_at(0.0, 0.5) < 0
