"""Actual signing boundary cannot use an earlier ground-ready snapshot."""

import pytest

from planner.models import CommandOperation
from relay.audit import SessionAuditLog
from relay.contracts import NodeType
from relay.session import RelayLimits, RelaySession
from relay.state import RegistryError
from relay.tests.conftest import SESSION, EventIds, MutableClock
from relay.tests.test_ground_node_foundation import GROUND_KEY, _ground_join, _ground_readiness


@pytest.fixture
def ready_session(tmp_path):
    clock = MutableClock()
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=clock,
        event_ids=EventIds(),
        node_types={9: NodeType.GROUND},
    )
    session.registry.apply_join(_ground_join(9, "joined"))
    session.registry.apply_ground_pose_observation(
        drone_id=9,
        connection_epoch=1,
        event_id="pose-1",
        session=SESSION,
        source_id="ohmni-pose",
        frame="odom",
        t=clock(),
    )
    session.registry.apply_readiness(_ground_readiness(9, "ready"))
    assert session.current_state()["drones"][0]["selectable"]
    return session, clock


def issue(session, operation):
    args = (
        {"linear_mm_s": 100, "angular_mrad_s": 0, "duration_ms": 100}
        if operation is CommandOperation.GROUND_VELOCITY
        else {"return_id": "approved-return"}
        if operation is CommandOperation.GROUND_RETURN
        else {}
    )
    return session.issue_command(
        command_id="release-test",
        intent_id="release-intent",
        roster_version=session.registry.roster_version,
        drone_id=9,
        connection_epoch=1,
        operation=operation,
        args=args,
        signing_key=GROUND_KEY,
    )


@pytest.mark.parametrize(
    "operation", [CommandOperation.GROUND_VELOCITY, CommandOperation.GROUND_RETURN]
)
@pytest.mark.parametrize("change", ["stale", "future", "invalid_pose", "authority", "estop"])
def test_motion_release_revalidates_after_ready_snapshot(ready_session, operation, change):
    session, clock = ready_session
    if change == "stale":
        clock.advance(1001)
    elif change == "future":
        clock.advance(-1)
    elif change == "invalid_pose":
        session.registry.clear_ground_pose_observation(drone_id=9, connection_epoch=1)
    elif change == "authority":
        session.registry.apply_readiness(_ground_readiness(9, "withdraw", authority=False))
    else:
        session.registry.set_estop(True)
    with pytest.raises(RegistryError):
        issue(session, operation)
    assert not session._issued_commands


@pytest.mark.parametrize("operation", [CommandOperation.HOVER, CommandOperation.ESTOP])
def test_ground_stop_still_releases_after_pose_and_authority_loss(ready_session, operation):
    session, clock = ready_session
    clock.advance(1001)
    session.registry.clear_ground_pose_observation(drone_id=9, connection_epoch=1)
    session.registry.set_estop(True)
    assert issue(session, operation)["operation"] == operation.value


def test_ready_ground_release_is_supported(ready_session):
    session, _ = ready_session
    assert issue(session, CommandOperation.GROUND_VELOCITY)["operation"] == "ground_velocity"


def test_rejoin_invalidates_media_epoch_and_node_status(ready_session):
    session, clock = ready_session
    old_epoch, _ = session.registry.media_context(9)
    session.registry.disconnect(drone_id=9, connection_epoch=1, t=clock(), event_id="disconnected")
    from dataclasses import replace

    clock.advance(100)
    session.registry.apply_join(replace(_ground_join(9, "rejoined"), t=clock()))
    new_epoch, status = session.registry.media_context(9)
    assert new_epoch == clock() and new_epoch > old_epoch
    assert status is None


def test_readiness_uses_host_receipt_for_accepted_pose_age(ready_session):
    session, clock = ready_session
    clock.advance(100)
    session.registry.apply_ground_pose_observation(
        drone_id=9,
        connection_epoch=1,
        event_id="pose-1",
        session=SESSION,
        source_id="ohmni-pose",
        frame="odom",
        t=clock(),
    )
    # The signed request was generated before its pose reached this relay.
    transition = session.registry.apply_readiness(
        _ground_readiness(9, "delayed-ready"), received_at=clock() + 1
    )
    assert transition.membership.value == "ready"
    assert transition.readiness_reasons == ()
    session.registry.check_ground_release(9, 1, now_ms=clock() + 1)


def test_five_authenticated_ground_units_join_without_synthetic_readiness(tmp_path):
    from relay.auth import Principal
    from relay.settings import AdapterBackend, RelaySettings
    from relay.tests.conftest import CONSOLE_KEY, membership_payload

    keys = {
        device_id: f"ground-unit-{device_id}-independent-test-key-000000".encode()
        for device_id in range(11, 16)
    }
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys=keys,
        node_types={device_id: NodeType.GROUND for device_id in keys},
        adapter_backend=AdapterBackend.REMOTE,
        log_dir=tmp_path,
    )
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=MutableClock(),
        event_ids=EventIds(),
        node_types=settings.node_types,
    )
    assert session.current_state()["drones"] == []
    for device_id, key in keys.items():
        session.process_membership(
            membership_payload(
                action="join",
                event_id=f"join-{device_id}",
                drone_id=device_id,
                key=key,
                node_type="ground",
                capabilities=["ground_drive"],
            ),
            Principal("adapter", device_id, key),
        )
    nodes = session.current_state()["drones"]
    assert {node["drone_id"] for node in nodes} == set(keys)
    assert len(nodes) == 5
    for node in nodes:
        assert node["node_type"] == "ground"
        assert node["membership"] == "registered"
        assert node["control_authority"] is False
        assert node["selectable"] is False
        assert node["ground_readiness"] == {"source_id": None}


def test_configured_fleet_and_intent_bounds_remain_independent():
    from relay.intent_v1 import AcceptedIntent, RejectedIntent, validate_intent
    from relay.settings import AdapterBackend, RelaySettings, SettingsError
    from relay.tests.conftest import CONSOLE_KEY, intent_payload

    keys = {
        device_id: f"mixed-unit-{device_id}-independent-test-key-000000".encode()
        for device_id in range(1, 65)
    }
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys=keys,
        node_types={device_id: NodeType.GROUND for device_id in range(33, 65)},
        physical_aircraft_limit=32,
        adapter_backend=AdapterBackend.REMOTE,
    )
    assert len(settings.adapter_keys) == 64
    with pytest.raises(SettingsError, match="64"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={**keys, 65: b"unit-65-independent-test-key-00000000000"},
        )
    raw = {**intent_payload(), "selection": list(range(1, 33))}
    assert isinstance(validate_intent(raw), AcceptedIntent)
    assert isinstance(validate_intent({**raw, "selection": list(range(1, 34))}), RejectedIntent)
