from dataclasses import replace

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.test_dispatch import ExecutingOnceFlight, translate_plan
from planner.models import LifecycleStatus, MembershipState, Plan
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot, make_stack


@pytest.mark.parametrize("target_change", ["epoch", "disconnected", "missing"])
def test_final_completion_cannot_ignore_changed_target_after_roster_change(target_change):
    snapshot = make_snapshot(1)
    plan = translate_plan(snapshot)
    _, _, arbiter, _, _, camera = make_stack(snapshot)
    flight = ExecutingOnceFlight.from_snapshot(snapshot)
    dispatcher = AdapterDispatcher(flight=flight, camera=camera, arbiter=arbiter)
    pending = dispatcher.dispatch(plan, snapshot)
    terminal = replace(pending.acknowledgements[-1], status=LifecycleStatus.COMPLETED)
    aircraft = dict(snapshot.aircraft)
    if target_change == "missing":
        aircraft.pop(1)
    elif target_change == "epoch":
        aircraft[1] = replace(aircraft[1], connection_epoch=aircraft[1].connection_epoch + 1)
    else:
        aircraft[1] = replace(aircraft[1], membership=MembershipState.DISCONNECTED)
    changed = replace(snapshot, roster_version=snapshot.roster_version + 1, aircraft=aircraft)

    result = dispatcher.resume_after_completion(plan, pending, terminal, changed)

    assert result.status is LifecycleStatus.INVALIDATED
    assert result.refusal is not None
    assert sum(call.operation.value == "goto" for call in flight.calls) == 1


def test_final_camera_completion_after_join_cannot_drop_capture_evidence():
    snapshot = make_snapshot(1)
    _, planner, _, dispatcher, _, _ = make_stack(snapshot)
    plan = planner.plan(
        make_intent(
            IntentName.CAPTURE_ROOM,
            selection=(1,),
            args={"room_id": "room", "capture_id": "capture", "pattern": "pano_360"},
            confirm=True,
        ),
        snapshot,
    )
    assert isinstance(plan, Plan)
    completed = dispatcher.dispatch(plan, snapshot)
    assert completed.status is LifecycleStatus.COMPLETED
    terminal = completed.acknowledgements[-1]
    pending = replace(
        completed,
        status=LifecycleStatus.EXECUTING,
        capture_bundle=None,
        acknowledgements=(
            *completed.acknowledgements[:-1],
            replace(terminal, status=LifecycleStatus.ACCEPTED),
        ),
    )
    changed = replace(snapshot, roster_version=snapshot.roster_version + 1)

    result = dispatcher.resume_after_completion(plan, pending, terminal, changed)

    assert result.status is LifecycleStatus.INVALIDATED
    assert result.refusal is not None
