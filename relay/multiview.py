"""Frozen platform reviews for sequential stationary multiview captures."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from relay.navigation_service import NavigationError, NavigationService

_MAX_VIEWS = 8


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise NavigationError("invalid_request", f"{field} must be a bounded identifier.", 400)
    return value


@dataclass(slots=True)
class _View:
    viewpoint_id: str
    zone_id: str
    capture_id: str
    review: dict[str, object]
    review_hash: str
    state: str = "planned"
    detail: str = "Route review is frozen and awaiting confirmation."


@dataclass(slots=True)
class _Workflow:
    session: str
    intent_id: str
    preview_hash: str
    expires_at_ms: int
    views: list[_View]
    state: str = "pending_confirmation"
    current: int = 0


class MultiviewService:
    """Coordinates one confirmed aircraft route and capture at a time.

    The navigation service remains the authority for every child route review and
    confirmation. This owner only retains their relationship and never makes a
    browser-supplied route executable.
    """

    def __init__(self, navigation: NavigationService, execution: object | None = None) -> None:
        self.navigation = navigation
        self.execution = execution
        self._workflows: dict[str, _Workflow] = {}
        self._children: dict[str, tuple[str, int, str]] = {}
        self._lock = threading.RLock()

    def preview(self, session: str, raw: object) -> dict[str, object]:
        if not isinstance(raw, Mapping) or set(raw) != {"intentId", "selected", "viewpoints"}:
            raise NavigationError(
                "invalid_request", "Request fields do not match the contract.", 400
            )
        intent_id = _identity(raw["intentId"], "intentId")
        viewpoints = raw["viewpoints"]
        if not isinstance(viewpoints, list) or not 1 <= len(viewpoints) <= _MAX_VIEWS:
            raise NavigationError("invalid_request", "Provide one to eight viewpoints.", 400)
        context = self.navigation.multiview_context(session)
        selected = context["selected"]
        if (
            raw["selected"] != selected
            or len(selected) != 1
            or selected[0].get("deviceClass") != "aircraft"
        ):
            raise NavigationError(
                "selection_changed",
                "Select exactly one current aircraft before previewing multiview capture.",
            )
        views: list[_View] = []
        seen_viewpoints: set[str] = set()
        seen_captures: set[str] = set()
        roster_version = context["rosterVersion"]
        for ordinal, item in enumerate(viewpoints, start=1):
            if not isinstance(item, Mapping) or set(item) != {"viewpointId", "zoneId", "captureId"}:
                raise NavigationError(
                    "invalid_request", "Each viewpoint fields do not match the contract.", 400
                )
            viewpoint_id = _identity(item["viewpointId"], "viewpointId")
            zone_id = _identity(item["zoneId"], "zoneId")
            capture_id = _identity(item["captureId"], "captureId")
            if viewpoint_id in seen_viewpoints or capture_id in seen_captures:
                raise NavigationError(
                    "invalid_request", "Viewpoint and capture IDs must be unique.", 400
                )
            seen_viewpoints.add(viewpoint_id)
            seen_captures.add(capture_id)
            child_intent = f"mv-{_digest([intent_id, viewpoint_id])[:24]}-{ordinal}"
            result = self.navigation.preview(
                session,
                {
                    "session": session,
                    "intentId": child_intent,
                    "zoneId": zone_id,
                    "rosterVersion": roster_version,
                    "selected": selected,
                    **{
                        key: context[key]
                        for key in ("catalogVersion", "map", "configVersion", "motionConfig")
                    },
                },
            )
            preview = result["preview"]
            if not isinstance(preview, dict) or preview.get("dispatchEligible") is not True:
                raise NavigationError(
                    "multiview_route_unavailable", "Every multiview route must be qualified."
                )
            views.append(_View(viewpoint_id, zone_id, capture_id, preview, result["previewHash"]))
        expires_at = min(int(view.review["expiresAt"]) for view in views)
        preview_id = str(uuid.uuid4())
        response = self._preview_response(preview_id, intent_id, expires_at, views)
        preview_hash = _digest(response)
        with self._lock:
            self._workflows[preview_id] = _Workflow(
                session, intent_id, preview_hash, expires_at, views
            )
        return {**response, "previewHash": preview_hash}

    def confirm(self, session: str, raw: object) -> dict[str, object]:
        if not isinstance(raw, Mapping) or set(raw) != {"previewId", "intentId", "previewHash"}:
            raise NavigationError(
                "invalid_request", "Request fields do not match the contract.", 400
            )
        preview_id = _identity(raw["previewId"], "previewId")
        with self._lock:
            workflow = self._workflows.get(preview_id)
            if workflow is None or workflow.session != session:
                raise NavigationError("preview_unknown", "No matching multiview review exists.")
            if workflow.intent_id != raw["intentId"] or workflow.preview_hash != raw["previewHash"]:
                raise NavigationError(
                    "confirmation_mismatch", "Confirmation does not bind the captured review."
                )
            if workflow.state != "pending_confirmation":
                raise NavigationError(
                    "confirmation_consumed", "This multiview review has already been consumed."
                )
            workflow.state = "navigating"
            view = workflow.views[0]
            views = tuple(workflow.views)
        try:
            for reserved in views:
                response = self.navigation.reserve(
                    session,
                    {
                        "previewId": reserved.review["previewId"],
                        "intentId": reserved.review["intentId"],
                        "previewHash": reserved.review_hash,
                    },
                )
                if response.get("status") != "accepted":
                    raise ValueError("qualified navigation reservation was refused")
        except (KeyError, TypeError, ValueError) as error:
            with self._lock:
                workflow.state = "failed"
                view.state, view.detail = "failed", str(error) or "Route reservation failed."
            raise NavigationError("multiview_reservation_failed", view.detail) from None
        self._dispatch_navigation(session, preview_id, 0, view)
        return {"status": "accepted", "code": "multiview_accepted", "workflowId": preview_id}

    def observe_execution(
        self, session: str, intent_id: str, intent_name: str, status: str
    ) -> None:
        """Advance only after the child result was committed to the relay ledger."""
        with self._lock:
            child = self._children.pop(intent_id, None)
            if child is None:
                return
            workflow_id, index, kind = child
            workflow = self._workflows.get(workflow_id)
            if (
                workflow is None
                or workflow.session != session
                or workflow.state in {"failed", "completed"}
            ):
                return
            view = workflow.views[index]
            if status != "completed":
                workflow.state = "failed"
                view.state, view.detail = "failed", f"{kind} ended {status}."
                return
            if kind == "navigation":
                view.state, view.detail = (
                    "arrival_verified",
                    "Arrival completed; capture is awaiting dispatch.",
                )
                if self.execution is None:
                    workflow.state = "failed"
                    view.state, view.detail = "failed", "Platform capture execution is unavailable."
                    return
                capture_intent = f"platform-capture:{intent_id}"
                self._children[capture_intent] = (workflow_id, index, "capture")
                try:
                    result = self.execution.confirm_platform_capture(
                        session,
                        {
                            "captureId": view.capture_id,
                            "roomId": view.zone_id,
                            "navigationIntentId": intent_id,
                            "selected": view.review["selected"],
                        },
                    )
                    if (
                        result.get("status") != "accepted"
                        or result.get("intentId") != capture_intent
                    ):
                        raise ValueError("platform capture was not accepted")
                    view.state, view.detail = (
                        "capturing",
                        "Fresh arrival evidence admitted the still capture.",
                    )
                except (ValueError, KeyError, TypeError) as error:
                    self._children.pop(capture_intent, None)
                    workflow.state = "failed"
                    view.state, view.detail = (
                        "failed",
                        str(error) or "Platform capture dispatch failed.",
                    )
                return
            view.state, view.detail = "completed", "Still capture and retrieval completed."
            next_index = index + 1
            if next_index == len(workflow.views):
                workflow.state = "completed"
                return
            workflow.current = next_index
            workflow.state = "navigating"
            next_view = workflow.views[next_index]
        self._dispatch_navigation(session, workflow_id, next_index, next_view)

    def status(self, session: str, workflow_id: str) -> dict[str, object]:
        with self._lock:
            workflow = self._workflows.get(workflow_id)
            if workflow is None or workflow.session != session:
                raise NavigationError(
                    "workflow_unknown", "No matching multiview workflow exists.", 404
                )
            return {
                "workflowId": workflow_id,
                "intentId": workflow.intent_id,
                "status": workflow.state,
                "views": [
                    {
                        "viewpointId": view.viewpoint_id,
                        "zoneId": view.zone_id,
                        "captureId": view.capture_id,
                        "state": view.state,
                        "detail": view.detail,
                    }
                    for view in workflow.views
                ],
            }

    def _dispatch_navigation(self, session: str, workflow_id: str, index: int, view: _View) -> None:
        child_intent = str(view.review["intentId"])
        with self._lock:
            self._children[child_intent] = (workflow_id, index, "navigation")
        response = self.navigation.dispatch_reserved(session, str(view.review["previewId"]))
        if response["status"] != "accepted":
            with self._lock:
                self._children.pop(child_intent, None)
                workflow = self._workflows[workflow_id]
                workflow.state = "failed"
                view.state, view.detail = "failed", str(response["detail"])
            return
        with self._lock:
            view.state, view.detail = "navigating", "The qualified route was dispatched."

    @staticmethod
    def _preview_response(
        preview_id: str, intent_id: str, expires_at: int, views: list[_View]
    ) -> dict[str, object]:
        first = views[0].review
        execution = first["execution"]
        return {
            "previewId": preview_id,
            "intentId": intent_id,
            "expiresAt": expires_at,
            "execution": execution,
            "views": [
                {
                    "viewpointId": view.viewpoint_id,
                    "zoneId": view.zone_id,
                    "captureId": view.capture_id,
                    "route": view.review["routes"][0],
                    "capture": {"roomId": view.zone_id, "pattern": "single_still"},
                }
                for view in views
            ],
        }
