from copy import deepcopy

import pytest

from relay.capabilities import IntentName
from relay.multiview import MultiviewService
from relay.navigation_service import NavigationError


class _Navigation:
    now = 10_000

    def clock_ms(self):
        return self.now

    def multiview_context(self, session):
        assert session == "s"
        return {
            "rosterVersion": 7,
            "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}],
            "catalogVersion": "catalog-1",
            "map": {"mapPin": {"version": "m1", "contentSha256": "a" * 64}},
            "configVersion": "config-1",
            "motionConfig": {"speed": 1},
        }

    def preview(self, session, request):
        zone = request["zoneId"]
        preview = {
            "previewId": f"preview-{zone}",
            "intentId": request["intentId"],
            "selected": request["selected"],
            "expiresAt": 10_100,
            "dispatchEligible": True,
            "execution": {
                "planHash": "b" * 64,
                "mapPin": {"version": "m1", "contentSha256": "a" * 64},
                "geometryPin": {"version": "g1", "contentSha256": "c" * 64},
                "navigationPin": {"version": "n1", "contentSha256": "d" * 64},
                "approvalId": "approval-1",
                "configurationSha256": "e" * 64,
                "permissionZoneIds": [zone],
            },
            "routes": [
                {
                    "target": {"id": 1, "deviceClass": "aircraft", "epoch": 2},
                    "waypoints": [],
                    "arrivalSlot": {"slotId": f"slot-{zone}", "zoneId": zone, "position": {}},
                    "holdBehavior": "hover",
                }
            ],
        }
        return {"preview": preview, "previewHash": "f" * 64}

    def preview_from_trusted_start(self, session, request, trusted_start):
        self.trusted_starts = getattr(self, "trusted_starts", []) + [deepcopy(trusted_start)]
        return self.preview(session, request)

    def reserve(self, session, request):
        self.reserved = getattr(self, "reserved", []) + [deepcopy(request)]
        return {"status": "accepted", "code": "navigation_accepted", "detail": "accepted"}

    def dispatch_reserved(self, session, preview_id):
        self.confirmed = {
            "previewId": preview_id,
            "intentId": next(
                item["intentId"] for item in self.reserved if item["previewId"] == preview_id
            ),
        }
        return {"status": "accepted", "code": "navigation_accepted", "detail": "accepted"}


def test_platform_multiview_has_a_dedicated_parent_intent() -> None:
    assert IntentName.MULTIVIEW_CAPTURE.value == "multiview_capture"


def test_multiview_freezes_individual_route_reviews_and_confirms_the_first_only() -> None:
    navigation = _Navigation()
    service = MultiviewService(navigation)
    preview = service.preview(
        "s",
        {
            "intentId": "multiview-1",
            "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}],
            "viewpoints": [
                {"viewpointId": "north", "zoneId": "north-zone", "captureId": "capture-north"},
                {"viewpointId": "south", "zoneId": "south-zone", "captureId": "capture-south"},
            ],
        },
    )

    assert preview["views"][0]["capture"] == {"roomId": "north-zone", "pattern": "single_still"}
    assert preview["views"][1]["route"]["arrivalSlot"]["slotId"] == "slot-south-zone"
    assert navigation.trusted_starts == [
        {
            "target": {"id": 1, "deviceClass": "aircraft", "epoch": 2},
            "position": {},
        }
    ]
    assert preview["serverNowMs"] == navigation.now
    accepted = service.confirm(
        "s",
        {key: preview[key] for key in ("previewId", "intentId", "previewHash")},
    )

    assert accepted == {
        "status": "accepted",
        "code": "multiview_accepted",
        "workflowId": preview["previewId"],
    }
    assert navigation.confirmed["previewId"] == "preview-north-zone"
    assert service.status("s", preview["previewId"])["views"][1]["state"] == "planned"


class _Execution:
    def __init__(self):
        self.captures = []

    def confirm_platform_capture(self, session, capture):
        self.captures.append((session, capture))
        return {
            "status": "accepted",
            "intentId": f"platform-capture:{capture['navigationIntentId']}",
        }


def test_multiview_dispatches_capture_only_after_navigation_then_advances() -> None:
    navigation = _Navigation()
    execution = _Execution()
    service = MultiviewService(navigation, execution)
    preview = service.preview(
        "s",
        {
            "intentId": "multiview-2",
            "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}],
            "viewpoints": [
                {"viewpointId": "north", "zoneId": "north-zone", "captureId": "capture-north"},
                {"viewpointId": "south", "zoneId": "south-zone", "captureId": "capture-south"},
            ],
        },
    )
    service.confirm("s", {key: preview[key] for key in ("previewId", "intentId", "previewHash")})
    first_navigation = navigation.confirmed["intentId"]
    assert first_navigation in service._children

    service.observe_execution("s", first_navigation, "navigate", "completed")
    assert service.status("s", preview["previewId"])["views"][0]["state"] == "capturing"
    assert execution.captures == [
        (
            "s",
            {
                "captureId": "capture-north",
                "roomId": "north-zone",
                "navigationIntentId": first_navigation,
                "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}],
            },
        )
    ]
    service.observe_execution(
        "s", f"platform-capture:{first_navigation}", "capture_room", "completed"
    )

    assert navigation.confirmed["previewId"] == "preview-south-zone"
    second_navigation = navigation.confirmed["intentId"]
    service.observe_execution("s", second_navigation, "navigate", "completed")
    service.observe_execution(
        "s", f"platform-capture:{second_navigation}", "capture_room", "completed"
    )
    assert service.status("s", preview["previewId"])["status"] == "completed"


def test_multiview_stops_after_a_preempted_navigation() -> None:
    navigation = _Navigation()
    execution = _Execution()
    service = MultiviewService(navigation, execution)
    preview = service.preview(
        "s",
        {
            "intentId": "multiview-stop",
            "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}],
            "viewpoints": [
                {"viewpointId": "north", "zoneId": "north-zone", "captureId": "capture-north"}
            ],
        },
    )
    service.confirm("s", {key: preview[key] for key in ("previewId", "intentId", "previewHash")})
    service.observe_execution("s", navigation.confirmed["intentId"], "navigate", "invalidated")

    status = service.status("s", preview["previewId"])
    assert status["status"] == "failed"
    assert status["views"][0]["state"] == "failed"
    assert execution.captures == []


def test_multiview_confirmation_refuses_an_expired_parent_review() -> None:
    navigation = _Navigation()
    service = MultiviewService(navigation)
    preview = service.preview(
        "s",
        {
            "intentId": "multiview-expired",
            "selected": [{"id": 1, "deviceClass": "aircraft", "epoch": 2}],
            "viewpoints": [
                {"viewpointId": "north", "zoneId": "north-zone", "captureId": "capture-north"}
            ],
        },
    )
    navigation.now = 10_100

    with pytest.raises(NavigationError, match="no longer current") as error:
        service.confirm(
            "s", {key: preview[key] for key in ("previewId", "intentId", "previewHash")}
        )
    assert error.value.code == "preview_expired"
