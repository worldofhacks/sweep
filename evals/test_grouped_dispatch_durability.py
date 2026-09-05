import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evals.test_m14_button_to_sim import CONSOLE_KEY, SESSION, Clock, Harness
from planner.models import FlightState
from relay.audit import AuditLogError, SessionAuditLog
from relay.auth import Principal
from relay.session import RelaySession
from tests.autonomy_fixtures import make_snapshot


@pytest.fixture
def admitted_group(tmp_path: Path) -> Iterator[tuple[Harness, RelaySession, str, str]]:
    harness = Harness(
        tmp_path,
        make_snapshot(
            1, selection=(1,), flight_state=FlightState.HOVERING, armed=True, now_ms=Clock().value
        ),
        auto_start_nodes=True,
    )
    with TestClient(harness.app) as client:
        runtime = harness.app.state.relay_runtime
        session = runtime.session(SESSION)
        assert client.portal is not None
        client.portal.call(runtime.stop)
        console = Principal(source="console", drone_id=None, signing_key=CONSOLE_KEY)
        keyboard = Principal(source="keyboard", drone_id=None, signing_key=CONSOLE_KEY)
        select = harness.intent("select", selection=[], args={"ids": [1]})
        session.process_frame(select, console)
        session.mark_pending_intent_delivered(select["intent_id"])
        assert session.execute_pending_intent(select["intent_id"])[-1]["status"] == "completed"
        motion = harness.intent("translate", selection=[1], args={"dx": 1, "dy": 0})
        stop = harness.intent("estop", selection=[], source="keyboard")
        for intent, principal in ((motion, console), (stop, keyboard)):
            assert session.process_frame(intent, principal)[0]["status"] == "accepted"
            session.mark_pending_intent_delivered(intent["intent_id"])
        yield harness, session, motion["intent_id"], stop["intent_id"]


def test_grouped_estop_is_durable_before_its_own_worker_starts(admitted_group) -> None:
    harness, session, motion_id, stop_id = admitted_group

    events = session.execute_pending_intent(motion_id)

    assert session.current_state()["estop"] is True
    assert any(
        event.get("intent_id") == stop_id and event.get("status") == "completed" for event in events
    )
    records = SessionAuditLog(session.audit_log.path.parent, SESSION).replay()
    assert any(
        record["event"].get("intent_id") == stop_id and record["event"].get("status") == "completed"
        for record in records
    )
    with sqlite3.connect(session.audit_log.database_path) as database:
        assert (
            database.execute("SELECT COUNT(*) FROM operations WHERE status = 'pending'").fetchone()[
                0
            ]
            == 0
        )
    calls = list(harness.flight.calls)
    assert session.execute_pending_intent(stop_id)[-1]["status"] == "completed"
    assert harness.flight.calls == calls


def test_crash_during_grouped_estop_leaves_its_own_pending_marker(
    admitted_group, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, session, motion_id, stop_id = admitted_group
    observed: list[int] = []
    real_estop = harness.flight.estop

    class AbruptStop(BaseException):
        pass

    def crash_after_estop():
        result = real_estop()
        assert result
        operation_ids = [
            session._pending_intents[intent_id].operation_id for intent_id in (motion_id, stop_id)
        ]
        assert None not in operation_ids
        assert len(set(operation_ids)) == 2
        with sqlite3.connect(session.audit_log.database_path) as database:
            observed.append(
                database.execute(
                    "SELECT COUNT(*) FROM operations WHERE status = 'pending' AND id IN (?, ?)",
                    operation_ids,
                ).fetchone()[0]
            )
        raise AbruptStop

    monkeypatch.setattr(harness.flight, "estop", crash_after_estop)
    with pytest.raises((AbruptStop, AuditLogError)):
        session.execute_pending_intent(motion_id)
    assert observed == [2]
    with pytest.raises(AuditLogError, match="incomplete operation"):
        SessionAuditLog(session.audit_log.path.parent, SESSION).replay()
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.current_state()
