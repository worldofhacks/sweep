from types import SimpleNamespace

import pytest

from language.compiler import _terminal_postcondition_matches
from language.contracts import ProposedIntent
from planner.models import Command, CommandOperation, Plan
from relay.intent_v1 import IntentName, Mode


@pytest.mark.parametrize(
    ("name", "plan"),
    [
        (IntentName.TAKEOFF, None),
        (
            IntentName.TRANSLATE,
            Plan(
                plan_id="plan:translate",
                intent_id="translate",
                intent_name=IntentName.TRANSLATE,
                roster_version=1,
                selection=(1,),
                confirmed=True,
                commands=(
                    Command(
                        command_id="command:translate",
                        intent_id="translate",
                        roster_version=1,
                        drone_id=1,
                        connection_epoch=1,
                        operation=CommandOperation.GOTO,
                        parameters={"x": 2.0, "y": 0.0, "z": 1.0, "speed": 0.5},
                    ),
                ),
            ),
        ),
        (
            IntentName.COME_HOME,
            Plan(
                plan_id="plan:come-home",
                intent_id="come-home",
                intent_name=IntentName.COME_HOME,
                roster_version=1,
                selection=(1,),
                confirmed=True,
                commands=(
                    Command(
                        command_id="command:come-home",
                        intent_id="come-home",
                        roster_version=1,
                        drone_id=1,
                        connection_epoch=1,
                        operation=CommandOperation.GOTO,
                        parameters={"x": 0.0, "y": 0.0, "z": 1.0, "speed": 0.5},
                    ),
                ),
            ),
        ),
    ],
)
def test_airborne_is_a_terminal_completion_state_for_motion(
    name: IntentName, plan: Plan | None
) -> None:
    intent = ProposedIntent(name=name, args={}, selection=(1,), mode=Mode.INDOOR)
    after = SimpleNamespace(
        drones=(
            {
                "drone_id": 1,
                "flight_state": "airborne",
                "position": (2.0, 0.0, 1.0) if name is IntentName.TRANSLATE else (0.0, 0.0, 1.0),
            },
        )
    )

    assert _terminal_postcondition_matches(intent, after, after, execution_plan=plan)


def test_motion_completion_rejects_noncanonical_moving_state() -> None:
    intent = ProposedIntent(name=IntentName.TAKEOFF, args={}, selection=(1,), mode=Mode.INDOOR)
    facts = SimpleNamespace(drones=({"drone_id": 1, "flight_state": "moving"},))

    assert not _terminal_postcondition_matches(intent, facts, facts, execution_plan=None)
