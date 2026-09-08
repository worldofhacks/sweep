from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from relay.navigation_service import NavigationError
from relay.tests.test_navigation_service import Case, FlightExecution, live_state, routes_for


@pytest.mark.parametrize("operation", ["compile", "preview", "route_review", "confirm"])
def test_world_map_read_can_finish_while_navigation_waits_for_its_provider(tmp_path, operation):
    entered, release = Event(), Event()

    def pause():
        entered.set()
        if not release.wait(3):
            raise ValueError("world map read did not finish")

    class WaitingExecution(FlightExecution):
        def preview(self, session, preview):
            if operation != "confirm":
                pause()
            return super().preview(session, preview)

        def confirm(self, session, preview):
            pause()
            return super().confirm(session, preview)

    def route_provider(*args):
        pause()
        return routes_for(*args)

    case = Case(tmp_path, flight_execution=WaitingExecution())
    case.state = live_state(("aircraft",))
    if operation == "route_review":
        case.service.flight_execution = None
        case.service.route_preview = route_provider
    try:
        if operation == "confirm":
            request = case.confirmation(case.preview())
            action = case.service.confirm
        elif operation in {"preview", "route_review"}:
            request = case.request()
            action = case.service.preview
        else:
            request = {"intentId": "concurrent", "query": "Room A"}
            action = case.service.compile
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = workers.submit(action, "test-session", request)
            try:
                assert entered.wait(2), "navigation did not reach its provider"
                world_map = workers.submit(case.service.current_approved_bundle, "test-session")
                assert world_map.result(timeout=1) == case.approved
                if operation == "confirm":
                    repeated = workers.submit(case.service.confirm, "test-session", request)
                    assert repeated.result(timeout=1)["code"] == "confirmation_consumed"
            finally:
                release.set()
            result = pending.result(timeout=2)
            if operation == "confirm":
                assert result["status"] == "accepted"
            else:
                assert result["preview"]["routes"]
    finally:
        case.service.close()


def test_observed_state_roundtrip_during_provider_work_retires_the_review(tmp_path):
    entered, release = Event(), Event()

    class WaitingExecution(FlightExecution):
        def preview(self, session, preview):
            entered.set()
            if not release.wait(3):
                raise ValueError("state publication did not finish")
            return super().preview(session, preview)

    case = Case(tmp_path, flight_execution=WaitingExecution())
    case.state = live_state(("aircraft",))

    def publish_roundtrip():
        changed = copy.deepcopy(case.state)
        changed["drones"][0]["control_authority"] = False
        case.service.observe_state("test-session", changed)
        case.service.observe_state("test-session", case.state)

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = workers.submit(case.preview)
            try:
                assert entered.wait(2), "navigation did not reach its provider"
                workers.submit(publish_roundtrip).result(timeout=1)
            finally:
                release.set()
            with pytest.raises(NavigationError) as error:
                pending.result(timeout=2)
            assert error.value.code == "frozen_inputs_changed"
    finally:
        case.service.close()


def test_failed_dispatch_keeps_confirmation_consumed_after_unlocking(tmp_path):
    entered, release = Event(), Event()

    class FailingExecution(FlightExecution):
        def confirm(self, session, preview):
            entered.set()
            if not release.wait(3):
                raise ValueError("world map read did not finish")
            raise ValueError("qualified world pose was withdrawn")

    case = Case(tmp_path, flight_execution=FailingExecution())
    case.state = live_state(("aircraft",))
    try:
        request = case.confirmation(case.preview())
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending = workers.submit(case.service.confirm, "test-session", request)
            try:
                assert entered.wait(2), "confirmation did not reach its provider"
                repeated = workers.submit(case.service.confirm, "test-session", request)
                assert repeated.result(timeout=1)["code"] == "confirmation_consumed"
            finally:
                release.set()
            result = pending.result(timeout=2)
            assert result["status"] == "refused"
            assert result["code"] == "navigation_dispatch_failed"
            assert result["dispatchEligible"] is False
        assert case.service.confirm("test-session", request)["code"] == "confirmation_consumed"
    finally:
        case.service.close()
