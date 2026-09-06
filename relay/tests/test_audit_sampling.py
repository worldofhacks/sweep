"""Decision-grade telemetry retention and bounded state audit sampling."""

from __future__ import annotations

from pathlib import Path

import pytest

from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import Principal
from relay.contracts import MembershipRequest, parse_membership_request
from relay.session import (
    MAX_AUDIT_STATE_INTERVAL_MS,
    RelayLimits,
    RelaySession,
    _material_state_projection,
)
from relay.state import MAX_MEMBERSHIP_HISTORY_LIMIT, FleetRegistry
from relay.tests.conftest import (
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
    node_status_payload,
    profiled_sink,
    telemetry_payload,
)


def _session(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, **limits: int
) -> RelaySession:
    return RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(
            intent_max_age_ms=5_000,
            transport_event_max_age_ms=5_000,
            future_clock_skew_ms=1_000,
            telemetry_freshness_ms=1_000,
            **limits,
        ),
        clock=clock,
        event_ids=event_ids,
        intent_sink=profiled_sink(lambda _intent, _state: None),
    )


def _join(session: RelaySession, principal: Principal, event_id: str = "join-1") -> None:
    session.process_membership(
        membership_payload(action="join", event_id=event_id, timestamp=session.clock()),
        principal,
    )


def _audited(session: RelaySession) -> list[dict[str, object]]:
    return [record["event"] for record in session.replay()["events"]]


def _audited_types(session: RelaySession) -> list[str]:
    return [str(event["type"]) for event in _audited(session)]


def _telemetry(
    session: RelaySession, principal: Principal, event_id: str, **changes: object
) -> list[dict[str, object]]:
    payload = telemetry_payload(event_id=event_id, timestamp=session.clock(), state="landed")
    payload.update(changes)
    return session.process_telemetry(payload, principal)


@pytest.mark.parametrize("value", [True, 1.5, 0, MAX_AUDIT_STATE_INTERVAL_MS + 1, 10**100])
def test_state_sampling_interval_is_bounded(value: object) -> None:
    limits: dict[str, object] = {
        "intent_max_age_ms": 5_000,
        "transport_event_max_age_ms": 5_000,
        "future_clock_skew_ms": 1_000,
        "telemetry_freshness_ms": 1_000,
        "audit_state_interval_ms": value,
    }
    with pytest.raises(ValueError, match="audit_state_interval_ms"):
        RelayLimits(**limits)  # type: ignore[arg-type]


def test_every_accepted_telemetry_is_retained_while_state_is_sampled(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)
    started = clock.value

    for index in range(30):  # 10 Hz for three seconds, landed, moving only in x
        events = _telemetry(session, adapter_principal, f"telemetry-{index}", x=float(index))
        assert [event["type"] for event in events] == ["telemetry", "state"]
        clock.advance(100)

    audited = _audited(session)
    telemetry = [event for event in audited if event["type"] == "telemetry"]
    states = [event for event in audited if event["type"] == "state"]
    assert [event["event_id"] for event in telemetry] == [
        f"telemetry-{index}" for index in range(30)
    ]
    assert [event["t"] - started for event in telemetry] == [index * 100 for index in range(30)]
    assert len(states) == 2  # join and the first transition from no telemetry to landed
    assert session.metrics()["telemetry_events"] == 30


def test_flight_state_change_audits_a_new_state_without_dropping_other_telemetry(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)
    _telemetry(session, adapter_principal, "landed-1")
    clock.advance(100)
    _telemetry(session, adapter_principal, "landed-2", x=2.0)
    clock.advance(100)

    _telemetry(session, adapter_principal, "hovering-1", state="hovering")
    clock.advance(100)
    _telemetry(session, adapter_principal, "hovering-2", state="hovering", x=3.0)

    audited = _audited(session)
    assert [event["type"] for event in audited] == [
        "membership",
        "state",
        "telemetry",
        "state",
        "telemetry",
        "telemetry",
        "state",
        "telemetry",
    ]
    assert [event["event_id"] for event in audited if event["type"] == "telemetry"] == [
        "landed-1",
        "landed-2",
        "hovering-1",
        "hovering-2",
    ]
    assert [event for event in audited if event["type"] == "state"][-1]["drones"][0][
        "flight_state"
    ] == "hovering"


def test_telemetry_that_changes_readiness_is_audited_with_its_transition(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)
    _telemetry(session, adapter_principal, "telemetry-1")
    session.process_membership(
        membership_payload(action="readiness", event_id="ready-1", timestamp=clock.value),
        adapter_principal,
    )
    clock.advance(1_001)
    stale = session.periodic_events()
    clock.advance(100)

    recovered = _telemetry(session, adapter_principal, "telemetry-2")

    assert [event["type"] for event in stale] == ["membership", "state"]
    assert [event["type"] for event in recovered] == ["telemetry", "membership", "state"]
    audited = _audited(session)
    assert [event["type"] for event in audited[-5:]] == [
        "membership",
        "state",
        "telemetry",
        "membership",
        "state",
    ]
    assert [event["action"] for event in audited if event["type"] == "membership"] == [
        "join",
        "readiness",
        "telemetry_stale",
        "telemetry_recovered",
    ]
    assert audited[-3]["event_id"] == "telemetry-2"


def test_idle_state_keepalive_is_audited_at_most_once_per_interval(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids, audit_state_interval_ms=2_000)
    _join(session, adapter_principal)
    started = clock.value

    for _ in range(45):
        clock.advance(100)
        assert [event["type"] for event in session.periodic_events()] == ["state"]

    states = [event for event in _audited(session) if event["type"] == "state"]
    assert [event["t"] - started for event in states] == [0, 2_000, 4_000]
    assert session.registry.state_event(session=SESSION, t=0, event_id="x")["state_sequence"] > 45


def test_relay_clock_regression_resets_state_sampling_baseline(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids, audit_state_interval_ms=2_000)
    _join(session, adapter_principal)
    started = clock.value
    clock.advance(1_000)
    session.periodic_events()

    clock.advance(-2_000)
    session.periodic_events()
    clock.advance(100)
    session.periodic_events()

    states = [event for event in _audited(session) if event["type"] == "state"]
    assert [event["t"] - started for event in states] == [0, -1_000]


def test_relay_clock_regression_does_not_suppress_accepted_telemetry(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)
    started = clock.value
    _telemetry(session, adapter_principal, "telemetry-1")

    clock.advance(-500)
    session.process_telemetry(
        telemetry_payload(event_id="telemetry-2", timestamp=started + 100, state="landed"),
        adapter_principal,
    )
    clock.advance(100)
    session.process_telemetry(
        telemetry_payload(event_id="telemetry-3", timestamp=started + 200, state="landed"),
        adapter_principal,
    )

    telemetry = [event for event in _audited(session) if event["type"] == "telemetry"]
    assert [event["event_id"] for event in telemetry] == [
        "telemetry-1",
        "telemetry-2",
        "telemetry-3",
    ]


def test_transient_decision_input_remains_in_replay_after_immediate_recovery(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)

    _telemetry(session, adapter_principal, "critical-battery", battery=0.05)
    decision_snapshot = session.current_state()
    assert decision_snapshot["drones"][0]["battery"] == 0.05
    clock.advance(100)
    _telemetry(session, adapter_principal, "recovered-battery", battery=0.9)
    session.update_control_projection(selection=(1,))

    telemetry = [event for event in _audited(session) if event["type"] == "telemetry"]
    assert [(event["event_id"], event["battery"]) for event in telemetry] == [
        ("critical-battery", 0.05),
        ("recovered-battery", 0.9),
    ]


def test_state_is_audited_when_a_node_report_changes_it_and_not_for_a_duplicate(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)
    clock.advance(100)
    session.process_frame(
        node_status_payload(event_id="status-1", timestamp=clock.value, watchdog_state="hold"),
        adapter_principal,
    )
    clock.advance(100)
    session.process_frame(
        node_status_payload(event_id="status-2", timestamp=clock.value, watchdog_state="hold"),
        adapter_principal,
    )
    clock.advance(100)
    session.process_frame(
        node_status_payload(event_id="status-3", timestamp=clock.value, watchdog_state="nominal"),
        adapter_principal,
    )

    assert _audited_types(session) == [
        "membership",
        "state",
        "node_status",
        "state",
        "node_status",
        "node_status",
        "state",
    ]


def test_video_frame_timestamp_is_volatile_but_video_status_is_material() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    registry.apply_join(_join_request("join-1", 1_000))
    first = registry.state_event(session=SESSION, t=1_000, event_id="state-1")
    second = registry.state_event(session=SESSION, t=1_100, event_id="state-2")
    first_drone = first["drones"][0]
    second_drone = second["drones"][0]
    first_drone["video"] = {"status": "live", "last_frame_at": 1_000}
    second_drone["video"] = {"status": "live", "last_frame_at": 1_100}

    assert _material_state_projection(first) == _material_state_projection(second)

    second_drone["video"] = {"status": "offline", "last_frame_at": 1_100}
    assert _material_state_projection(first) != _material_state_projection(second)


def test_control_decisions_always_audit_the_state_they_produced(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)

    decided = [
        session.update_control_projection(selection=(1,)),
        session.update_control_projection(selection=(1,)),
        session.update_control_projection(estop=True),
    ]

    assert _audited_types(session) == ["membership", "state", "state", "state", "state"]
    audited_states = [event for event in _audited(session) if event["type"] == "state"]
    assert audited_states[-3:] == decided


def test_material_state_projection_fails_closed_for_unbounded_or_non_json_extensions() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    state = registry.state_event(session=SESSION, t=1_000, event_id="state-1")

    state["captures"] = []
    with pytest.raises(AuditLogError, match="bounded audit projectors: captures"):
        _material_state_projection(state)

    state.pop("captures")
    registry.apply_join(_join_request("join-projection", 1_000))
    state = registry.state_event(session=SESSION, t=1_000, event_id="state-2")
    state["drones"][0]["unbounded_future_history"] = [{"value": index} for index in range(10_000)]
    with pytest.raises(AuditLogError, match="unknown unbounded_future_history"):
        _material_state_projection(state)

    state["drones"][0].pop("unbounded_future_history")
    state["drones"][0]["adapter_capabilities"] = ["flight"] * 65
    with pytest.raises(AuditLogError, match="adapter_capabilities.*at most 64"):
        _material_state_projection(state)

    state = registry.state_event(session=SESSION, t=1_000, event_id="state-3")
    state["drones"][0]["video"]["future_samples"] = []
    with pytest.raises(AuditLogError, match="video fields"):
        _material_state_projection(state)

    state["drones"] = []
    state["accepted_plan"] = {"unsafe": object()}
    with pytest.raises(AuditLogError, match="material state is not JSON-native"):
        _material_state_projection(state)


def test_sampling_baseline_rolls_back_with_a_failed_operation(
    tmp_path: Path,
    clock: MutableClock,
    event_ids: EventIds,
    adapter_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session(tmp_path, clock, event_ids)
    _join(session, adapter_principal)
    _telemetry(session, adapter_principal, "telemetry-1")
    clock.advance(100)
    before = session._audit_sampling.copy()

    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise AuditLogError("disk full")

    monkeypatch.setattr(session.audit_log, "append_batch", disk_full)
    with pytest.raises(AuditLogError, match="disk full"):
        _telemetry(session, adapter_principal, "telemetry-2", state="hovering")

    assert session._audit_sampling == before


def _join_request(event_id: str, timestamp: int) -> MembershipRequest:
    return parse_membership_request(
        membership_payload(action="join", event_id=event_id, timestamp=timestamp)
    )


def test_state_frames_keep_only_the_newest_membership_transitions() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000, membership_history_limit=2)
    registry.apply_join(_join_request("join-1", 1_000))
    registry.disconnect(drone_id=1, connection_epoch=1, t=1_100, event_id="loss-1")
    registry.apply_join(_join_request("join-2", 1_200))

    drone = registry.state_event(session=SESSION, t=1_300, event_id="state-1")["drones"][0]

    assert [entry["membership"] for entry in drone["membership_history"]] == [
        "disconnected",
        "registered",
    ]
    assert drone["membership_history_truncated"] == 1
    assert len(registry._aircraft[1].history) == 2
    assert registry._aircraft[1].history_truncated == 1


def test_default_history_bound_and_rejected_limits() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000)
    registry.apply_join(_join_request("join-0", 1_000))
    for index in range(1, 6):
        registry.disconnect(
            drone_id=1, connection_epoch=index, t=1_000 + index, event_id=f"loss-{index}"
        )
        registry.apply_join(_join_request(f"join-{index}", 1_000 + index))

    drone = registry.state_event(session=SESSION, t=2_000, event_id="state-1")["drones"][0]

    assert registry.membership_history_limit == 8
    assert len(drone["membership_history"]) == 8
    assert drone["membership_history_truncated"] == 3
    assert drone["membership_history"][-1]["connection_epoch"] == 6
    assert len(registry._aircraft[1].history) == 8
    assert registry._aircraft[1].history_truncated == 3
    for limit in (0, -1, True, 2.5, MAX_MEMBERSHIP_HISTORY_LIMIT + 1):
        with pytest.raises(ValueError, match="membership_history_limit"):
            FleetRegistry(telemetry_freshness_ms=1_000, membership_history_limit=limit)  # type: ignore[arg-type]


def test_bounded_history_transaction_rollback_restores_evicted_transition() -> None:
    registry = FleetRegistry(telemetry_freshness_ms=1_000, membership_history_limit=2)
    registry.apply_join(_join_request("join-1", 1_000))
    registry.disconnect(drone_id=1, connection_epoch=1, t=1_100, event_id="loss-1")
    before = registry.state_event(session=SESSION, t=1_150, event_id="before")["drones"][0]

    with pytest.raises(RuntimeError, match="abort"):
        with registry.transaction():
            registry.apply_join(_join_request("join-2", 1_200))
            raise RuntimeError("abort")

    after = registry.state_event(session=SESSION, t=1_250, event_id="after")["drones"][0]
    assert after["connection_epoch"] == 1
    assert after["membership"] == "disconnected"
    assert after["membership_history"] == before["membership_history"]
    assert after["membership_history_truncated"] == before["membership_history_truncated"] == 0


def test_truncated_frames_keep_every_transition_in_the_audited_membership_records(
    tmp_path: Path, clock: MutableClock, event_ids: EventIds, adapter_principal: Principal
) -> None:
    session = _session(tmp_path, clock, event_ids, state_membership_history=1)
    _join(session, adapter_principal, "join-1")
    clock.advance(100)
    session.handle_adapter_disconnect(drone_id=1, connection_epoch=1)
    clock.advance(100)
    rejoined = session.process_membership(
        membership_payload(action="join", event_id="join-2", timestamp=clock.value),
        adapter_principal,
    )

    drone = rejoined[1]["drones"][0]
    assert [entry["membership"] for entry in drone["membership_history"]] == ["registered"]
    assert drone["membership_history_truncated"] == 2
    audited = _audited(session)
    assert [event["action"] for event in audited if event["type"] == "membership"] == [
        "join",
        "unexpected_loss",
        "join",
    ]
    assert [
        event["drones"][0]["membership_history_truncated"]
        for event in audited
        if event["type"] == "state"
    ] == [0, 1, 2]
