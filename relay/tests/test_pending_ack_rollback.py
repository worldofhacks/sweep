from types import SimpleNamespace

import pytest

from planner.models import CommandOperation
from relay.audit import AuditLogError
from relay.auth import Principal
from relay.contracts import LifecycleStatus
from relay.session import RelaySession
from relay.tests.conftest import (
    acknowledgement_payload,
    intent_payload,
    membership_payload,
    profiled_sink,
)


def register_command(session: RelaySession) -> None:
    session.register_dispatched_command(
        SimpleNamespace(
            command_id="command-1",
            intent_id="intent-1",
            roster_version=1,
            drone_id=1,
            connection_epoch=1,
            operation=CommandOperation.HOVER,
        )
    )


def test_disk_full_does_not_retain_uncommitted_early_completion(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = relay_session
    resumed = []

    class Sink:
        def __call__(self, intent: object, state: object) -> None:
            session.process_acknowledgement(
                acknowledgement_payload(event_id="early-completion", status="completed"),
                adapter_principal,
            )

        def resume_after_acknowledgement(
            self, relay: RelaySession, acknowledgement: object
        ) -> None:
            resumed.append(acknowledgement)

    session.intent_sink = profiled_sink(Sink())
    session.process_membership(
        membership_payload(action="join", event_id="join"), adapter_principal
    )
    session.process_intent(intent_payload(), console_principal)
    register_command(session)

    def disk_full(*args: object, **kwargs: object) -> None:
        raise AuditLogError("disk full")

    monkeypatch.setattr(session.audit_log, "append_batch", disk_full)
    with pytest.raises(AuditLogError):
        session.execute_pending_intent("intent-1")

    assert session._pending_intents["intent-1"].acknowledgements == []
    assert session._intents["intent-1"].command_statuses == {}
    assert session._acknowledgements == {}
    assert resumed == []
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.execute_pending_intent("intent-1")


@pytest.mark.parametrize("failure", ["begin", "complete", "continuation"])
def test_disk_full_preserves_phased_completion_ownership(
    relay_session: RelaySession,
    adapter_principal: Principal,
    console_principal: Principal,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    session = relay_session
    calls = []
    token = SimpleNamespace(intent_id="intent-1")
    next_token = SimpleNamespace(intent_id="recovery-stop")

    class Sink:
        def __call__(self, intent: object, state: object) -> None:
            return None

        def resume_after_acknowledgement(self, *args: object) -> None:
            pytest.fail("phased completion must not use legacy resume")

        def prepare_resume(self, relay: RelaySession, acknowledgement: object) -> object:
            calls.append("prepare")
            return token

        def resume_io(self, actual_token: object) -> object:
            assert actual_token is token
            calls.append("io")
            return "adapter-result"

        def commit_resume(self, actual_token: object, outcome: object) -> object:
            assert actual_token is token
            assert outcome == "adapter-result"
            calls.append("commit")
            event = session.record_lifecycle(
                intent_id="intent-1", status=LifecycleStatus.COMPLETED, source="planner"
            )
            return SimpleNamespace(
                relay_events=[event],
                continuation=next_token if failure == "continuation" else None,
            )

    session.intent_sink = profiled_sink(Sink())
    session.process_membership(
        membership_payload(action="join", event_id="join"), adapter_principal
    )
    session.process_intent(intent_payload(), console_principal)
    register_command(session)
    session.execute_pending_intent("intent-1", defer_resume=True)
    session.process_acknowledgement(
        acknowledgement_payload(event_id="completion", status="completed"),
        adapter_principal,
        defer_resume=True,
    )
    queue = session._acknowledgements["intent-1"]
    queued = queue.copy()

    def disk_full(*args: object, **kwargs: object) -> None:
        raise AuditLogError("disk full")

    if failure == "begin":
        monkeypatch.setattr(session.audit_log, "begin_operation", disk_full)
        with pytest.raises(AuditLogError, match="disk full"):
            session.prepare_resume("intent-1")
        assert session._acknowledgements["intent-1"] is queue
        assert queue == queued
        assert session._resuming_intents == set()
        with pytest.raises(AuditLogError, match="session is unusable"):
            session.prepare_resume("intent-1")
        assert calls == ["prepare"]
        return

    work = session.prepare_resume("intent-1")
    assert work is not None
    blocked_ids = work.blocked_ids.copy()
    assert session._resuming_intents == blocked_ids
    before_status = session._intents["intent-1"].status
    outcome = session.resume_io(work)
    monkeypatch.setattr(session.audit_log, "append_batch", disk_full)
    with pytest.raises(AuditLogError, match="disk full"):
        session.commit_resume(work, outcome)

    assert calls == ["prepare", "io", "commit"]
    assert session._acknowledgements["intent-1"] is queue
    assert queue == queued
    assert session._resuming_intents == blocked_ids
    assert session._resume_continuations == []
    assert work.token is token
    assert work.blocked_ids == blocked_ids
    assert session._intents["intent-1"].status is before_status
    with pytest.raises(AuditLogError, match="session is unusable"):
        session.prepare_resume("intent-1")
    assert calls == ["prepare", "io", "commit"]
