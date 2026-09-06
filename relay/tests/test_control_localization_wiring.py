from __future__ import annotations

import asyncio

from relay.app import RelayRuntime
from relay.audit import SessionAuditLog
from relay.auth import Principal
from relay.control_frames import sign_localization_frame
from relay.control_localization import ControlLocalizationWire, to_wire_payload
from relay.session import RelayLimits, RelaySession
from relay.settings import RelaySettings
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
)
from relay.tests.test_control_localization import fresh_snapshot, mapping, store

LOCALIZATION_KEY = b"localization-key-at-least-32-bytes"


def test_localization_credentials_resolve_from_the_documented_environment(tmp_path) -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": f'{{"1":"{ADAPTER_KEY.decode()}"}}',
            "SWEEP_LOCALIZATION_KEYS_JSON": '{"1":"localization-key-at-least-32-bytes"}',
            "SWEEP_SESSION_LOG_DIR": str(tmp_path),
        }
    )

    assert settings.credential_resolver().resolve("localization", 1) == (
        b"localization-key-at-least-32-bytes"
    )


def test_localization_frames_are_not_fanned_out_to_console_subscribers(tmp_path) -> None:
    async def exercise() -> None:
        runtime = RelayRuntime(
            RelaySettings(relay_token=CONSOLE_KEY, adapter_keys={1: ADAPTER_KEY}, log_dir=tmp_path)
        )
        runtime.session(SESSION)
        subscription = await runtime.subscribe(SESSION, Principal("console", None, CONSOLE_KEY))

        await runtime.publish(
            SESSION,
            [
                {
                    "v": 1,
                    "type": "control_localization",
                    "event_id": "localization-1",
                    "session": SESSION,
                }
            ],
        )

        assert subscription.queue.empty()

    asyncio.run(exercise())


def test_signed_localization_is_applied_to_the_session_snapshot(tmp_path) -> None:
    clock = MutableClock(value=101_000)
    session = RelaySession(
        session_id=SESSION,
        audit_log=SessionAuditLog(tmp_path, SESSION),
        limits=RelayLimits(5000, 5000, 1000, 1000),
        clock=clock,
        event_ids=EventIds(),
        control_localization_store=store(),
    )
    adapter = Principal("adapter", 1, ADAPTER_KEY)
    session.process_membership(
        membership_payload(action="join", event_id="join", timestamp=clock()), adapter
    )
    session.process_membership(
        membership_payload(action="readiness", event_id="ready", timestamp=clock()), adapter
    )
    wire = ControlLocalizationWire.from_mapping(
        to_wire_payload(fresh_snapshot(), mapping(), "pending", "wired-localization")
    )
    raw = sign_localization_frame(
        wire,
        timestamp_ms=clock(),
        event_id=wire.event_id,
        session=SESSION,
        signing_key=LOCALIZATION_KEY,
    )

    accepted = session.process_frame(raw, Principal("localization", 1, LOCALIZATION_KEY))
    assert accepted[0]["type"] == "control_localization", accepted[0].get("reason")
    snapshot = session.apply_control_localization(fresh_snapshot_for_session())

    assert snapshot.aircraft[1].position_quality == 1.0


def fresh_snapshot_for_session():
    from tests.autonomy_fixtures import make_snapshot

    return make_snapshot(1, now_ms=101_000)
