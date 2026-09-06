from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from relay.auth import Principal
from relay.contracts import Membership, parse_membership_request, parse_node_status
from relay.media import (
    MediaEvidence,
    MediaMonitor,
    MediaMtxClient,
    MediaPathObservation,
    MediaUnreachable,
    parse_path_observation,
    project_video,
    stream_name,
)
from relay.session import RelaySession
from relay.state import FleetRegistry
from relay.tests.conftest import (
    SESSION,
    MutableClock,
    membership_payload,
    node_status_payload,
)

T0 = 1_756_700_000_000


def _status(state: str, *, timestamp: int = T0, drone_id: int = 1, epoch: int = 1):
    return parse_node_status(
        node_status_payload(
            event_id=f"status-{state}-{timestamp}",
            timestamp=timestamp,
            drone_id=drone_id,
            connection_epoch=epoch,
            video_publish_state=state,
        )
    )


def _evidence(*, online: bool, last_frame_at: int | None, fresh: bool, observed_at: int = T0):
    return MediaEvidence(
        online=online, last_frame_at=last_frame_at, observed_at=observed_at, fresh=fresh
    )


def test_node_claim_alone_maps_publishing_to_live_and_everything_else_to_offline() -> None:
    live = project_video(
        membership=Membership.READY,
        node_status=_status("publishing", timestamp=T0 + 500),
        node_publishing_at=T0 + 500,
        evidence=None,
    )
    assert live == {"status": "live", "last_frame_at": T0 + 500}

    for state in ("stopped", "connecting", "failed"):
        offline = project_video(
            membership=Membership.READY,
            node_status=_status(state, timestamp=T0 + 2_000),
            node_publishing_at=T0 + 500,
            evidence=None,
        )
        assert offline == {"status": "offline", "last_frame_at": T0 + 500}

    never = project_video(
        membership=Membership.READY,
        node_status=_status("stopped"),
        node_publishing_at=None,
        evidence=None,
    )
    assert never == {"status": "offline", "last_frame_at": None}

    unreported = project_video(
        membership=Membership.REGISTERED, node_status=None, node_publishing_at=None, evidence=None
    )
    assert unreported == {"status": "unreported", "last_frame_at": None}


def test_disconnected_aircraft_is_never_live_on_its_last_claim() -> None:
    lost = project_video(
        membership=Membership.DISCONNECTED,
        node_status=_status("publishing"),
        node_publishing_at=T0,
        evidence=None,
    )
    assert lost == {"status": "offline", "last_frame_at": T0}

    silent = project_video(
        membership=Membership.DISCONNECTED, node_status=None, node_publishing_at=None, evidence=None
    )
    assert silent == {"status": "unreported", "last_frame_at": None}


def test_fresh_mediamtx_evidence_decides_status_and_the_newest_evidence_dates_the_frame() -> None:
    live = project_video(
        membership=Membership.READY,
        node_status=_status("stopped"),
        node_publishing_at=None,
        evidence=_evidence(online=True, last_frame_at=T0 + 900, fresh=True),
    )
    assert live == {"status": "live", "last_frame_at": T0 + 900}

    dropped = project_video(
        membership=Membership.READY,
        node_status=_status("publishing", timestamp=T0 + 100),
        node_publishing_at=T0 + 100,
        evidence=_evidence(online=False, last_frame_at=T0 + 4_000, fresh=True),
    )
    assert dropped == {"status": "offline", "last_frame_at": T0 + 4_000}

    absent_path = project_video(
        membership=Membership.READY,
        node_status=_status("publishing", timestamp=T0 + 100),
        node_publishing_at=T0 + 100,
        evidence=_evidence(online=False, last_frame_at=None, fresh=True),
    )
    assert absent_path == {"status": "offline", "last_frame_at": T0 + 100}

    disconnected_but_streaming = project_video(
        membership=Membership.DISCONNECTED,
        node_status=None,
        node_publishing_at=None,
        evidence=_evidence(online=True, last_frame_at=T0 + 50, fresh=True),
    )
    assert disconnected_but_streaming["status"] == "live"


def test_stale_mediamtx_evidence_degrades_to_the_node_claim_and_never_to_a_false_live() -> None:
    stale_online = _evidence(online=True, last_frame_at=T0 + 900, fresh=False)
    assert project_video(
        membership=Membership.READY,
        node_status=_status("stopped", timestamp=T0 + 1_000),
        node_publishing_at=None,
        evidence=stale_online,
    ) == {"status": "offline", "last_frame_at": T0 + 900}
    assert project_video(
        membership=Membership.READY,
        node_status=None,
        node_publishing_at=None,
        evidence=stale_online,
    ) == {"status": "unreported", "last_frame_at": T0 + 900}
    assert project_video(
        membership=Membership.READY,
        node_status=_status("publishing", timestamp=T0 + 2_000),
        node_publishing_at=T0 + 2_000,
        evidence=_evidence(online=False, last_frame_at=T0 + 900, fresh=False),
    ) == {"status": "live", "last_frame_at": T0 + 2_000}


class FakePathClient:
    """Scripted MediaMTX: per-path documents, or an outage for the whole server."""

    def __init__(self) -> None:
        self.paths: dict[str, MediaPathObservation | None] = {}
        self.outage: Exception | None = None
        self.calls: list[str] = []
        self.closed = False

    async def read_path(self, name: str) -> MediaPathObservation | None:
        self.calls.append(name)
        if self.outage is not None:
            raise self.outage
        return self.paths.get(name)

    async def close(self) -> None:
        self.closed = True


def _run(coroutine):
    return asyncio.run(coroutine)


def test_monitor_dates_frames_by_growing_inbound_bytes_and_keeps_the_age_on_a_stall() -> None:
    clock = MutableClock(T0)
    client = FakePathClient()
    monitor = MediaMonitor(client, clock=clock, drone_ids=(1, 2), stale_after_ms=3_000)
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=1_000)

    assert _run(monitor.poll_once()) is True
    assert sorted(client.calls) == ["drone1", "drone2"]
    assert monitor.evidence(1, clock()) == MediaEvidence(
        online=True, last_frame_at=T0, observed_at=T0, fresh=True
    )
    assert monitor.evidence(2, clock()) == MediaEvidence(
        online=False, last_frame_at=None, observed_at=T0, fresh=True
    )
    assert monitor.evidence(3, clock()) is None

    clock.advance(1_000)
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=2_000)
    _run(monitor.poll_once())
    assert monitor.evidence(1, clock()).last_frame_at == T0 + 1_000

    clock.advance(1_000)
    _run(monitor.poll_once())
    stalled = monitor.evidence(1, clock())
    assert stalled.online is True
    assert stalled.last_frame_at == T0 + 1_000
    assert stalled.observed_at == T0 + 2_000

    clock.advance(1_000)
    client.paths["drone1"] = None
    _run(monitor.poll_once())
    assert monitor.evidence(1, clock()) == MediaEvidence(
        online=False, last_frame_at=T0 + 1_000, observed_at=T0 + 3_000, fresh=True
    )

    clock.advance(1_000)
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=None)
    _run(monitor.poll_once())
    assert monitor.evidence(1, clock()).last_frame_at == T0 + 4_000


def test_monitor_dates_a_counter_reset_as_new_path_evidence() -> None:
    clock = MutableClock(T0)
    client = FakePathClient()
    monitor = MediaMonitor(client, clock=clock, drone_ids=(1,), stale_after_ms=3_000)
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=10_000)
    _run(monitor.poll_once())

    clock.advance(1_000)
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=100)
    _run(monitor.poll_once())

    assert monitor.evidence(1, clock()).last_frame_at == T0 + 1_000


def test_monitor_outage_keeps_the_last_evidence_until_it_ages_out() -> None:
    clock = MutableClock(T0)
    client = FakePathClient()
    monitor = MediaMonitor(
        client, clock=clock, drone_ids=(1,), poll_interval_ms=1_000, stale_after_ms=3_000
    )
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=10)
    _run(monitor.poll_once())
    assert monitor.reachable is True

    client.outage = MediaUnreachable("connection refused")
    clock.advance(1_000)
    assert _run(monitor.poll_once()) is False
    assert monitor.reachable is False
    assert monitor.evidence(1, clock()).fresh is True

    clock.advance(2_000)
    _run(monitor.poll_once())
    assert monitor.evidence(1, clock()).fresh is True
    clock.advance(1)
    stale = monitor.evidence(1, clock())
    assert stale.fresh is False
    assert stale.online is True
    assert stale.last_frame_at == T0

    client.outage = ValueError("unexpected client failure")
    assert _run(monitor.poll_once()) is False

    client.outage = None
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=20)
    assert _run(monitor.poll_once()) is True
    assert monitor.reachable is True
    assert monitor.evidence(1, clock()) == MediaEvidence(
        online=True, last_frame_at=clock(), observed_at=clock(), fresh=True
    )


def test_monitor_fails_stale_when_the_runtime_clock_regresses() -> None:
    clock = MutableClock(T0)
    client = FakePathClient()
    monitor = MediaMonitor(client, clock=clock, drone_ids=(1,), stale_after_ms=3_000)
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=10)
    _run(monitor.poll_once())

    assert monitor.evidence(1, T0 - 1).fresh is False


def test_monitor_task_polls_on_its_own_and_stops_cleanly() -> None:
    clock = MutableClock(T0)
    client = FakePathClient()
    client.paths["drone1"] = MediaPathObservation(online=True, inbound_bytes=1)
    monitor = MediaMonitor(
        client, clock=clock, drone_ids=(1,), poll_interval_ms=1, stale_after_ms=1
    )

    async def scenario() -> None:
        await monitor.start()
        for _ in range(100):
            await asyncio.sleep(0.002)
            if len(client.calls) >= 2:
                break
        assert len(client.calls) >= 2
        await monitor.stop()
        calls_after_stop = len(client.calls)
        await asyncio.sleep(0.01)
        assert len(client.calls) == calls_after_stop

    _run(scenario())
    assert client.closed is True
    assert monitor.evidence(1, clock()).online is True


def test_monitor_rejects_a_stale_window_shorter_than_the_poll_interval() -> None:
    with pytest.raises(ValueError):
        MediaMonitor(
            FakePathClient(), clock=MutableClock(), poll_interval_ms=1_000, stale_after_ms=500
        )
    with pytest.raises(ValueError):
        MediaMonitor(FakePathClient(), clock=MutableClock(), poll_interval_ms=0)


def test_path_document_parsing_prefers_current_fields_and_accepts_deprecated_aliases() -> None:
    assert parse_path_observation(
        {"name": "drone1", "online": True, "inboundBytes": 42, "ready": False, "bytesReceived": 1}
    ) == MediaPathObservation(online=True, inbound_bytes=42)
    assert parse_path_observation(
        {"name": "drone1", "ready": True, "bytesReceived": 7}
    ) == MediaPathObservation(online=True, inbound_bytes=7)
    assert parse_path_observation({"online": False}) == MediaPathObservation(
        online=False, inbound_bytes=None
    )
    for malformed in (
        [],
        {"name": "drone1"},
        {"online": "yes"},
        {"online": True, "inboundBytes": "x"},
        {"online": True, "inboundBytes": -1},
    ):
        with pytest.raises(MediaUnreachable):
            parse_path_observation(malformed)


def _mediamtx(handler) -> MediaMtxClient:
    return MediaMtxClient(
        "http://127.0.0.1:9997",
        username="sweep-api",
        password="api-secret",
        timeout_s=0.5,
        transport=httpx.MockTransport(handler),
    )


def test_mediamtx_client_reads_the_path_with_basic_auth_and_a_bounded_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"name": "drone1", "online": True, "inboundBytes": 512, "readers": []}
        )

    client = _mediamtx(handler)

    async def scenario():
        try:
            return await client.read_path(stream_name(1))
        finally:
            await client.close()

    assert _run(scenario()) == MediaPathObservation(online=True, inbound_bytes=512)
    assert seen[0].url.path == "/v3/paths/get/drone1"
    expected = base64.b64encode(b"sweep-api:api-secret").decode()
    assert seen[0].headers["authorization"] == f"Basic {expected}"
    assert seen[0].extensions["timeout"]["read"] == 0.5


@pytest.mark.parametrize(
    ("respond", "expectation"),
    [
        (lambda _request: httpx.Response(404, json={"error": "path not found"}), None),
        (lambda _request: httpx.Response(401, text="unauthorized"), MediaUnreachable),
        (lambda _request: httpx.Response(500, json={"error": "boom"}), MediaUnreachable),
        (lambda _request: httpx.Response(200, text="{not json"), MediaUnreachable),
        (lambda _request: httpx.Response(200, json={"name": "drone1"}), MediaUnreachable),
        (lambda _request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), MediaUnreachable),
        (lambda _request: (_ for _ in ()).throw(httpx.ConnectError("refused")), MediaUnreachable),
    ],
)
def test_mediamtx_client_maps_every_failure_to_unreachable_and_a_missing_path_to_none(
    respond, expectation
) -> None:
    client = _mediamtx(respond)

    async def scenario():
        try:
            return await client.read_path("drone2")
        finally:
            await client.close()

    if expectation is None:
        assert _run(scenario()) is None
    else:
        with pytest.raises(expectation):
            _run(scenario())


def _join(registry: FleetRegistry, drone_id: int = 1) -> None:
    registry.apply_join(
        parse_membership_request(
            membership_payload(action="join", event_id=f"join-{drone_id}", drone_id=drone_id)
        )
    )


def _video(registry: FleetRegistry, now: int, drone_id: int = 1) -> dict[str, object]:
    state = registry.state_event(session=SESSION, t=now, event_id=f"state-{now}")
    return next(drone["video"] for drone in state["drones"] if drone["drone_id"] == drone_id)


def test_registry_projects_video_from_node_claims_and_keeps_frame_history_across_rejoin() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    _join(registry)
    assert _video(registry, T0) == {"status": "unreported", "last_frame_at": None}

    registry.apply_node_status(_status("stopped", timestamp=T0 + 100))
    assert _video(registry, T0 + 100) == {"status": "offline", "last_frame_at": None}

    registry.apply_node_status(_status("publishing", timestamp=T0 + 200))
    assert _video(registry, T0 + 250) == {"status": "live", "last_frame_at": T0 + 200}

    registry.apply_node_status(_status("failed", timestamp=T0 + 900))
    assert _video(registry, T0 + 950) == {"status": "offline", "last_frame_at": T0 + 200}

    registry.apply_node_status(_status("publishing", timestamp=T0 + 1_000))
    registry.disconnect(drone_id=1, t=T0 + 5_000, event_id="loss-1")
    assert _video(registry, T0 + 5_000) == {"status": "offline", "last_frame_at": T0 + 1_000}

    _join(registry)
    assert _video(registry, T0 + 6_000) == {"status": "unreported", "last_frame_at": T0 + 1_000}
    registry.apply_node_status(_status("publishing", timestamp=T0 + 6_100, epoch=2))
    assert _video(registry, T0 + 6_200) == {"status": "live", "last_frame_at": T0 + 6_100}


def test_registry_lets_fresh_mediamtx_evidence_override_the_node_claim() -> None:
    evidence: dict[int, MediaEvidence] = {}
    asked: list[tuple[int, int]] = []

    def provider(drone_id: int, now_ms: int) -> MediaEvidence | None:
        asked.append((drone_id, now_ms))
        return evidence.get(drone_id)

    registry = FleetRegistry(telemetry_freshness_ms=1_000, media_evidence=provider)
    _join(registry)
    registry.apply_node_status(_status("publishing", timestamp=T0))

    evidence[1] = _evidence(online=False, last_frame_at=None, fresh=True, observed_at=T0 + 300)
    assert _video(registry, T0 + 300) == {"status": "offline", "last_frame_at": T0}
    assert asked[-1] == (1, T0 + 300)

    evidence[1] = _evidence(online=True, last_frame_at=T0 + 1_200, fresh=True)
    assert _video(registry, T0 + 1_300) == {"status": "live", "last_frame_at": T0 + 1_200}

    evidence[1] = _evidence(online=True, last_frame_at=T0 + 1_200, fresh=False)
    registry.apply_node_status(_status("stopped", timestamp=T0 + 2_000))
    assert _video(registry, T0 + 9_000) == {"status": "offline", "last_frame_at": T0 + 1_200}


def test_session_state_carries_the_video_projection_for_the_console(
    relay_session: RelaySession, adapter_principal: Principal, clock: MutableClock
) -> None:
    relay_session.process_frame(
        membership_payload(action="join", event_id="join-1", timestamp=clock()), adapter_principal
    )
    events = relay_session.process_frame(
        node_status_payload(
            event_id="status-1", timestamp=clock(), video_publish_state="publishing"
        ),
        adapter_principal,
    )

    assert [event["type"] for event in events] == ["node_status", "state"]
    drone = events[1]["drones"][0]
    assert drone["video"] == {"status": "live", "last_frame_at": clock()}
    assert set(drone["video"]) == {"status", "last_frame_at"}
