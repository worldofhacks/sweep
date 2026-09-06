import asyncio

import pytest

import relay.session as session_module
from relay.app import RelayRuntime
from relay.audit import AuditLogError
from relay.auth import (
    AuthenticationError,
    Principal,
    StaticCredentialResolver,
    authenticate,
    sign_event,
    verify_event_signature,
)
from relay.control_frames import sign_localization_frame
from relay.control_localization import ControlLocalizationWire, to_wire_payload
from relay.settings import RelaySettings
from relay.tests.conftest import ADAPTER_KEY, membership_payload, telemetry_payload
from relay.tests.test_control_localization import mapping, projector, snapshot

LOCALIZATION_KEY = b"localization-test-key-32-characters"


def enable_projection(session) -> None:
    session.control_localization_projector = projector()
    session.control_pose_signing_key = lambda drone_id: ADAPTER_KEY if drone_id == 1 else None


def frame(session, *, event_id: str = "producer-pose-1", epoch: int = 1, **changes):
    body = to_wire_payload(snapshot(connection_epoch=epoch), mapping()) | changes
    wire = ControlLocalizationWire.from_mapping(body)
    return sign_localization_frame(
        wire,
        timestamp_ms=session.clock(),
        event_id=event_id,
        session=session.session_id,
        signing_key=LOCALIZATION_KEY,
    )


def join(session, adapter_principal) -> None:
    session.process_membership(
        membership_payload(action="join", event_id="join"),
        adapter_principal,
    )


def test_localization_uses_separate_aircraft_bound_credentials() -> None:
    resolver = StaticCredentialResolver(
        b"console-token-that-is-at-least-32-bytes",
        {1: ADAPTER_KEY},
        localization_keys={1: LOCALIZATION_KEY},
    )
    payload = {
        "v": 1,
        "type": "auth",
        "source": "localization",
        "drone_id": 1,
        "token": LOCALIZATION_KEY.decode(),
    }
    assert authenticate(payload, resolver) == Principal("localization", 1, LOCALIZATION_KEY)
    for invalid in (payload | {"drone_id": 2}, payload | {"token": ADAPTER_KEY.decode()}):
        with pytest.raises(AuthenticationError):
            authenticate(invalid, resolver)
    for invalid in (payload | {"drone_id": 2**31}, payload | {"token": "x" * 4_097}):
        with pytest.raises(AuthenticationError) as raised:
            authenticate(invalid, resolver)
        assert raised.value.code == "invalid_auth"


def test_session_emits_only_relay_signed_integer_non_approved_control_pose(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)

    events = relay_session.process_frame(frame(relay_session), producer)

    assert len(events) == 1
    outbound = events[0]
    pose = relay_session.control_pose(1)
    assert pose is not None
    signature = outbound["signature"]
    assert isinstance(signature, str)
    assert pose == relay_session.control_pose(1)
    assert pose.flight_approved is False
    assert all(type(outbound[field]) is int for field in ("x_mm", "y_mm", "z_mm"))
    assert "position_map_enu_m" not in outbound
    assert {**pose.unsigned_event(), "signature": signature} == outbound
    assert verify_event_signature(pose.unsigned_event(), signature, ADAPTER_KEY)
    assert not verify_event_signature(pose.unsigned_event(), signature, LOCALIZATION_KEY)
    audited = [record["event"] for record in relay_session.audit_log.replay()][-2:]
    assert [event["type"] for event in audited] == ["control_localization", "control_pose"]
    assert audited[0]["signature_verified"] is True
    assert audited[0]["projected_event_id"] == pose.event_id
    assert audited[1]["signature_emitted"] is True
    assert all("signature" not in event for event in audited)


def test_unconfigured_projection_and_missing_node_key_fail_closed(
    relay_session, adapter_principal
) -> None:
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)
    raw = frame(relay_session)

    assert relay_session.process_frame(raw, producer)[0]["reason"] == "localization_not_configured"
    relay_session.control_localization_projector = projector()
    assert (
        relay_session.process_frame(raw | {"event_id": "producer-pose-2"}, producer)[0]["reason"]
        == "invalid_signature"
    )
    missing_key = frame(relay_session, event_id="producer-pose-3")
    assert (
        relay_session.process_frame(missing_key, producer)[0]["reason"]
        == "control_pose_signing_key_missing"
    )
    relay_session.control_pose_signing_key = lambda _drone_id: b"short"
    invalid_key = frame(relay_session, event_id="producer-pose-4")
    assert (
        relay_session.process_frame(invalid_key, producer)[0]["reason"]
        == "control_pose_signing_key_missing"
    )
    assert relay_session.control_pose(1) is None


def test_demo_shared_adapter_token_is_not_used_to_sign_control_pose(
    tmp_path, clock, event_ids, adapter_principal
) -> None:
    settings = RelaySettings(
        relay_token=b"console-key-that-is-at-least-32-bytes",
        allow_shared_adapter_token=True,
        localization_keys={1: LOCALIZATION_KEY},
        log_dir=tmp_path,
    )
    runtime = RelayRuntime(
        settings,
        clock=clock,
        event_ids=event_ids,
        control_localization_factory=lambda _session: projector(),
    )
    session = runtime.session("session-test")
    join(session, adapter_principal)

    result = session.process_frame(
        frame(session),
        Principal("localization", 1, LOCALIZATION_KEY),
    )

    assert result[0]["reason"] == "control_pose_signing_key_missing"
    assert session.control_pose(1) is None

    class FalsySigningKey:
        def __bool__(self) -> bool:
            return False

        def __call__(self, drone_id: int) -> bytes | None:
            return ADAPTER_KEY if drone_id == 1 else None

    explicitly_keyed = RelayRuntime(
        settings,
        clock=clock,
        event_ids=event_ids,
        control_localization_factory=lambda _session: projector(),
        control_pose_signing_key=FalsySigningKey(),
    ).session("explicitly-keyed-session")
    explicitly_keyed.process_membership(
        membership_payload(
            action="join",
            event_id="explicitly-keyed-join",
            session="explicitly-keyed-session",
        ),
        adapter_principal,
    )
    explicitly_keyed_pose = explicitly_keyed.process_frame(
        frame(explicitly_keyed),
        Principal("localization", 1, LOCALIZATION_KEY),
    )[0]
    assert explicitly_keyed_pose["type"] == "control_pose"
    assert verify_event_signature(
        {key: value for key, value in explicitly_keyed_pose.items() if key != "signature"},
        explicitly_keyed_pose["signature"],
        ADAPTER_KEY,
    )


def test_strict_schema_is_verified_without_bool_or_normalization_ambiguity(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)

    valid = frame(relay_session)
    invalid_v = valid | {"v": True}
    unsigned = dict(invalid_v)
    unsigned.pop("signature")
    invalid_v["signature"] = sign_event(unsigned, LOCALIZATION_KEY)
    extra = valid | {"unexpected": "value"}
    unsigned = dict(extra)
    unsigned.pop("signature")
    extra["signature"] = sign_event(unsigned, LOCALIZATION_KEY)
    for invalid in (invalid_v, extra):
        assert relay_session.process_frame(invalid, producer)[0]["reason"] == "invalid_payload"


def test_correctly_signed_wrong_pins_are_refused_and_not_retained(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)

    result = relay_session.process_frame(
        frame(relay_session, map_id="wrong-map"),
        producer,
    )

    assert result[0]["reason"] == "localization_provenance_mismatch"
    assert relay_session.control_pose(1) is None


def test_replay_timing_and_epoch_are_enforced_before_retention(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)
    first = frame(relay_session)

    assert relay_session.process_frame(first, producer)[0]["type"] == "control_pose"
    assert relay_session.process_frame(first, producer)[0]["reason"] == "replayed_event"
    assert (
        relay_session.process_frame(frame(relay_session, event_id="epoch-2", epoch=2), producer)[0][
            "reason"
        ]
        == "stale_connection_epoch"
    )
    duplicate_state = frame(relay_session, event_id="same-state")
    assert (
        relay_session.process_frame(duplicate_state, producer)[0]["reason"]
        == "duplicate_localization_state"
    )


def test_transport_replay_ledger_is_hard_bounded_and_prunes_expired_entries(
    relay_session,
    adapter_principal,
    clock,
    monkeypatch,
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)
    monkeypatch.setattr(session_module, "_TRANSPORT_REPLAY_LEDGER_MAX", 8)
    monkeypatch.setattr(session_module, "_TRANSPORT_REPLAY_PRUNE_AT", 4)

    for index in range(7):
        relay_session.process_frame(
            frame(relay_session, event_id=f"bounded-producer-{index}"),
            producer,
        )
    assert len(relay_session._seen_transport_event_ids) == 8  # join plus seven frames

    full = relay_session.process_frame(
        frame(relay_session, event_id="bounded-overflow"),
        producer,
    )
    assert full[0]["reason"] == "transport_replay_capacity"
    assert len(relay_session._seen_transport_event_ids) == 8

    clock.advance(relay_session.limits.transport_event_max_age_ms + 1)
    after_expiry = relay_session.process_frame(
        frame(relay_session, event_id="bounded-after-expiry"),
        producer,
    )
    assert after_expiry[0]["reason"] != "transport_replay_capacity"
    assert relay_session._seen_transport_event_ids == {
        "bounded-after-expiry": clock() + relay_session.limits.transport_event_max_age_ms
    }


def test_relay_pose_event_id_cannot_collide_with_producer_identity(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    relay_session.event_ids = lambda: "producer-pose-1"

    result = relay_session.process_frame(
        frame(relay_session),
        Principal("localization", 1, LOCALIZATION_KEY),
    )

    assert result[0]["reason"] == "event_id_collision"
    assert relay_session.control_pose(1) is None


def test_localization_producer_cannot_emit_flight_or_adapter_frames(relay_session) -> None:
    producer = Principal("localization", 1, LOCALIZATION_KEY)
    for kind in ("intent", "telemetry", "membership", "acknowledgement", "control_pose"):
        assert (
            relay_session.process_frame({"type": kind}, producer)[0]["reason"]
            == "frame_not_allowed"
        )


def test_pose_retention_and_transport_claim_roll_back_with_failed_audit(
    relay_session,
    adapter_principal,
    monkeypatch,
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)
    payload = frame(relay_session)

    def fail(*args, **kwargs):
        raise AuditLogError("disk failed")

    monkeypatch.setattr(relay_session.audit_log, "append_batch", fail)
    with pytest.raises(AuditLogError):
        relay_session.process_frame(payload, producer)
    assert relay_session.control_pose(1) is None
    assert payload["event_id"] not in relay_session._seen_transport_event_ids


def test_localization_never_changes_adapter_telemetry_or_position_quality(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    telemetry = telemetry_payload(event_id="telemetry")
    telemetry["pos_quality"] = 0.0
    relay_session.process_telemetry(telemetry, adapter_principal)
    before = relay_session.current_state()["drones"][0]

    result = relay_session.process_frame(
        frame(relay_session),
        Principal("localization", 1, LOCALIZATION_KEY),
    )
    after = relay_session.current_state()["drones"][0]

    assert result[0]["type"] == "control_pose"
    assert before["pos_quality"] == after["pos_quality"] == 0.0
    assert before["telemetry"] == after["telemetry"]


def test_pose_free_hold_is_refused_and_the_reason_is_audited(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)
    raw = frame(
        relay_session,
        localization_status="hold",
        control_eligible=False,
        localization_confidence="red",
        localization_loss_age_s=1.0,
        localization_reason="tag_fix_missing",
        position_map_enu_m=None,
        covariance_map_enu_m2=None,
        fix_age_s=None,
    )

    result = relay_session.process_frame(raw, producer)

    assert result[0]["reason"] == "localization_pose_unavailable"
    assert relay_session.control_pose(1) is None
    assert relay_session.audit_log.replay()[-1]["event"]["reason"] == result[0]["reason"]


def test_forged_body_and_explicit_flight_approval_are_never_accepted(
    relay_session, adapter_principal
) -> None:
    enable_projection(relay_session)
    join(relay_session, adapter_principal)
    producer = Principal("localization", 1, LOCALIZATION_KEY)
    valid = frame(relay_session)

    forged = valid | {"position_map_enu_m": [99.0, 99.0, 99.0]}
    approved = frame(relay_session, event_id="approved") | {"flight_approved": True}
    for invalid in (forged, approved):
        assert relay_session.process_frame(invalid, producer)[0]["reason"] in {
            "invalid_signature",
            "invalid_payload",
        }
    assert relay_session.control_pose(1) is None


def test_runtime_allows_one_producer_and_targets_conflated_ready_pose_to_phone(
    tmp_path, clock, event_ids
) -> None:
    settings = RelaySettings(
        relay_token=b"console-key-that-is-at-least-32-bytes",
        adapter_keys={1: ADAPTER_KEY},
        localization_keys={1: LOCALIZATION_KEY},
        log_dir=tmp_path,
    )
    runtime = RelayRuntime(
        settings,
        clock=clock,
        event_ids=event_ids,
        control_localization_factory=lambda _session: projector(),
    )
    session = runtime.session("session-test")
    adapter = Principal("adapter", 1, ADAPTER_KEY)
    producer = Principal("localization", 1, LOCALIZATION_KEY)

    async def exercise() -> None:
        phone = await runtime.subscribe("session-test", adapter)
        source = await runtime.subscribe("session-test", producer)
        with pytest.raises(AuthenticationError, match="already bound"):
            await runtime.subscribe("session-test", producer)
        join(session, adapter)
        first = session.process_frame(frame(session), producer)[0]
        await runtime.publish("session-test", [first])
        second = {**first, "event_id": "relay-pose-new", "x_mm": 2_001}
        await runtime.publish("session-test", [second])

        assert source.queue.empty()
        assert phone.queue.qsize() == 1
        assert (await phone.queue.get()).event == second
        hold = {**second, "event_id": "relay-hold", "status": "hold"}
        newest = {**second, "event_id": "relay-pose-newest", "x_mm": 2_002}
        await runtime.publish("session-test", [second, hold, newest])
        assert [(await phone.queue.get()).event, (await phone.queue.get()).event] == [hold, newest]

        newer_hold = {**hold, "event_id": "relay-hold-newer"}
        land = {**hold, "event_id": "relay-land", "status": "land"}
        newer_land = {**land, "event_id": "relay-land-newer"}
        await runtime.publish("session-test", [second, hold, newer_hold])
        assert phone.queue.qsize() == 1
        assert (await phone.queue.get()).event == newer_hold
        await runtime.publish("session-test", [land, newer_hold])
        assert [(await phone.queue.get()).event, (await phone.queue.get()).event] == [
            land,
            newer_hold,
        ]
        await runtime.publish("session-test", [second, hold, land, newer_land])
        assert phone.queue.qsize() == 1
        assert (await phone.queue.get()).event == newer_land
        await runtime.unsubscribe("session-test", source)
        replacement = await runtime.subscribe("session-test", producer)
        await runtime.unsubscribe("session-test", replacement)
        await runtime.unsubscribe("session-test", phone)

    asyncio.run(exercise())
