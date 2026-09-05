from planner.coordination import resolve_intent_pair
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent, make_snapshot


def test_selected_landing_and_simultaneous_translation_require_safety_hold():
    snapshot = make_snapshot(3, selection=(1, 2))
    landing = make_intent(IntentName.LAND, intent_id="landing", selection=(1, 2))
    movement = make_intent(
        IntentName.TRANSLATE, intent_id="movement", selection=(1, 2), args={"dx": 1, "dy": 0}
    )
    result = resolve_intent_pair(landing, movement, snapshot, conflict_window_ms=100)
    assert result.accepted == ()
    assert {refusal.intent_id for refusal in result.refusals} == {"landing", "movement"}
    assert result.hold_required
