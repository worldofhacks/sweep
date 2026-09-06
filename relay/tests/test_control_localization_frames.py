from dataclasses import replace

import pytest

from relay.audit import AuditLogError
from relay.auth import AuthenticationError, Principal, StaticCredentialResolver, authenticate
from relay.control_frames import ControlLocalizationFrame, sign_localization_frame
from relay.control_localization import ClockMapping, ControlLocalizationWire, to_wire_payload
from relay.tests.conftest import membership_payload
from relay.tests.test_control_localization import fresh_snapshot

KEY = b"localization-test-key-32-characters"


def frame(session, *, event_id="control-pose", epoch=1):
    mapping = ClockMapping(
        "camera-clock", "relay-clock", 0.0, session.clock() - 1000, 1000.0, 10, True
    )
    wire = ControlLocalizationWire.from_mapping(
        to_wire_payload(fresh_snapshot(), mapping, "pending", event_id)
    )
    return sign_localization_frame(
        replace(wire, connection_epoch=epoch),
        timestamp_ms=session.clock(),
        event_id=event_id,
        session=session.session_id,
        signing_key=KEY,
    )


def test_localization_uses_separate_aircraft_bound_credentials():
    resolver = StaticCredentialResolver(
        b"console-token", {1: b"phone-token"}, localization_keys={1: KEY}
    )
    payload = dict(v=1, type="auth", source="localization", drone_id=1, token=KEY.decode())
    assert authenticate(payload, resolver) == Principal("localization", 1, KEY)
    for invalid in (payload | {"drone_id": 2}, payload | {"token": "phone-token"}):
        with pytest.raises(AuthenticationError):
            authenticate(invalid, resolver)


def test_only_signed_current_epoch_localization_is_retained(relay_session, adapter_principal):
    relay_session.process_membership(
        membership_payload(action="join", event_id="join"), adapter_principal
    )
    producer = Principal("localization", 1, KEY)
    payload = frame(relay_session)
    assert relay_session.process_frame(payload, producer)[0]["type"] == "control_localization"
    assert relay_session.control_localization(1) == ControlLocalizationFrame.parse(payload)
    assert relay_session.process_frame(payload, producer)[0]["reason"] == "replayed_event"
    forged = frame(relay_session, event_id="forged") | {"position_map_enu_m": [99, 99, 99]}
    assert relay_session.process_frame(forged, producer)[0]["reason"] == "invalid_signature"
    assert (
        relay_session.process_frame(frame(relay_session, epoch=2), producer)[0]["type"] == "refusal"
    )
    assert relay_session.control_localization(1).event_id == "control-pose"


def test_localization_producer_cannot_emit_flight_intents_or_adapter_telemetry(relay_session):
    producer = Principal("localization", 1, KEY)
    for kind in ("intent", "telemetry", "membership", "acknowledgement"):
        assert (
            relay_session.process_frame({"type": kind}, producer)[0]["reason"]
            == "frame_not_allowed"
        )


def test_localization_retention_rolls_back_with_failed_audit(
    relay_session,
    adapter_principal,
    monkeypatch,
):
    relay_session.process_membership(
        membership_payload(action="join", event_id="join"), adapter_principal
    )
    producer = Principal("localization", 1, KEY)
    payload = frame(relay_session)

    def fail(*args, **kwargs):
        raise AuditLogError("disk failed")

    monkeypatch.setattr(relay_session.audit_log, "append_batch", fail)
    with pytest.raises(AuditLogError):
        relay_session.process_frame(payload, producer)
    with pytest.raises(AuditLogError):
        relay_session.control_localization(1)
    assert payload["event_id"] not in relay_session._seen_transport_event_ids
