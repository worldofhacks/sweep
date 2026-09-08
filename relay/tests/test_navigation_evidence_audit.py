from __future__ import annotations

import pytest

from relay.app import RelayRuntime
from relay.auth import sign_event
from relay.contracts import parse_membership_request
from relay.settings import RelaySettings
from relay.tests.conftest import membership_payload
from relay.tests.test_navigation_wire import NODE_KEY, _publisher, _request


@pytest.fixture
def navigation_audit(tmp_path):
    publisher, plan, snapshots, _, clock = _publisher()
    runtime = RelayRuntime(
        RelaySettings(
            relay_token=b"navigation-audit-console-test-key-32",
            adapter_keys={1: NODE_KEY},
            log_dir=tmp_path,
        ),
        clock=clock,
    )
    session = runtime.session(publisher.session)
    session.registry.apply_join(
        parse_membership_request(
            membership_payload(
                action="join",
                event_id="joined",
                drone_id=1,
                session=publisher.session,
                timestamp=clock(),
            )
        )
    )
    with publisher.command_scope(plan, lambda: snapshots[0]):
        frames = publisher.prepare_request(_request(plan))
    return session, frames


def test_navigation_audit_retains_exact_route_and_pose_evidence_without_signatures(
    navigation_audit,
):
    session, frames = navigation_audit
    for frame in frames:
        session.record_navigation_evidence(frame)

    assert [record["event"] for record in session.audit_log.replay()] == [
        {
            **{name: value for name, value in frame.items() if name != "signature"},
            "signature_emitted": True,
        }
        for frame in frames
    ]
    assert all("signature" in frame for frame in frames)


def test_duplicate_navigation_identity_cannot_append_a_second_audit_record(navigation_audit):
    session, (route, pose) = navigation_audit
    session.record_navigation_evidence(route)

    with pytest.raises(ValueError, match="event_id has already been observed"):
        session.record_navigation_evidence(route)

    session.record_navigation_evidence(pose)
    assert [record["event"]["event_id"] for record in session.audit_log.replay()] == [
        route["event_id"],
        pose["event_id"],
    ]


@pytest.mark.parametrize("change", ["session", "epoch", "signature", "map_pin"])
def test_unbound_or_tampered_navigation_evidence_is_not_audited(navigation_audit, change):
    session, (original, _) = navigation_audit
    changed = dict(original)
    if change == "session":
        changed["session"] = "another-session"
    elif change == "epoch":
        changed["connection_epoch"] = 2
    elif change == "signature":
        changed["signature"] = "0" * 64
    else:
        changed["map_sha256"] = "0" * 64
    if change in {"session", "epoch"}:
        changed["signature"] = sign_event(
            {name: value for name, value in changed.items() if name != "signature"}, NODE_KEY
        )

    with pytest.raises(ValueError):
        session.record_navigation_evidence(changed)

    assert session.audit_log.replay() == []
    session.record_navigation_evidence(original)
    assert session.audit_log.replay()[0]["event"]["event_id"] == original["event_id"]
