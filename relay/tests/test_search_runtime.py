from __future__ import annotations

from perception.search_events import CameraPolicy
from planner.models import Plan, Refusal
from planner.navigation import ArtifactPin, NavigationPermission
from planner.search import SearchArea
from planner.test_navigation_runtime import _runtime, _snapshot
from relay.intent_v1 import IntentName
from relay.search_runtime import SearchMissionPreview, SearchRuntime, SearchRuntimeConfig
from tests.autonomy_fixtures import make_intent


def _search_runtime(*, map_pin: ArtifactPin | None = None) -> SearchRuntime:
    navigation = _runtime()
    artifact = navigation.artifact()
    return SearchRuntime(
        SearchRuntimeConfig(
            {"atrium": SearchArea("atrium", "level_1", ((0, 0), (8, 0), (8, 4), (0, 4)))},
            artifact.map_pin if map_pin is None else map_pin,
            CameraPolicy(90, 90, 1, -90, -90, 0, 0.25),
            "camera-calibration-v1",
            {1: "camera-1"},
            NavigationPermission(frozenset({"atrium"})),
        ),
        navigation,
    )


def _intent(intent_id: str = "search-runtime"):
    return make_intent(
        IntentName.NAVIGATE,
        selection=(1,),
        args={"zone_id": "atrium", "target_class": "backpack"},
        confirm=True,
        intent_id=intent_id,
    )


def test_search_prepare_pins_a_transit_plan_and_start_marks_it_running() -> None:
    runtime = _search_runtime()
    preview = runtime.prepare(_intent(), _snapshot())

    assert isinstance(preview, SearchMissionPreview)
    assert isinstance(preview.plan, Plan)
    assert preview.plan.navigation is not None
    assert preview.plan.navigation.route.map_pin == preview.search.map_pin
    assert runtime.start("search-runtime").state == "running"


def test_search_prepare_refuses_changed_configuration_map_and_duplicate_intent() -> None:
    snapshot = _snapshot()
    changed = _search_runtime(map_pin=ArtifactPin("other-map", "c" * 64)).prepare(
        _intent(), snapshot
    )
    runtime = _search_runtime()

    assert isinstance(changed, Refusal)
    assert "map pin changed" in changed.detail
    assert isinstance(runtime.prepare(_intent(), snapshot), SearchMissionPreview)
    duplicate = runtime.prepare(_intent(), snapshot)
    assert isinstance(duplicate, Refusal)
    assert "already has a mission" in duplicate.detail
