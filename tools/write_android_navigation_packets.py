"""Emit signed route packets for the Android bridge-core contract test."""

from __future__ import annotations

import json
import os
from pathlib import Path

from planner.models import CommandOperation, Plan
from planner.test_navigation_runtime import _intent, _runtime, _snapshot
from relay.auth import sign_event
from relay.contracts import command_event
from relay.navigation_control import NavigationControl, NavigationControlConfig
from relay.tests.test_navigation_control import KEY, _pose, _projector, _Session


def main() -> None:
    output = Path(
        os.environ.get("PYTHON_NAVIGATION_PACKET_PATH", "/tmp/python-navigation-packets.json")
    )
    runtime = _runtime()
    runtime.require_phone_authorization = True
    projector = _projector()
    runtime.configure_control_localization(
        projector.pins,
        max_fix_age_ms=projector.max_fix_age_ms,
        max_position_uncertainty_p95_m=projector.max_position_uncertainty_p95_m,
    )
    runtime.maximum_aircraft = 1
    control = NavigationControl(NavigationControlConfig(runtime, projector, "config", {1: KEY}))
    session = _Session(_pose())
    snapshot = control.approved_snapshot(_snapshot(), session)
    plan = runtime.prepare(_intent(), snapshot)
    assert isinstance(plan, Plan)
    command = next(item for item in plan.commands if item.operation is CommandOperation.GOTO)
    route = control.authorize(plan, command, snapshot, session.session_id)
    command_frame = command_event(
        t=snapshot.now_ms,
        event_id="python-command",
        session=session.session_id,
        command_id=command.command_id,
        intent_id=plan.intent_id,
        roster_version=plan.roster_version,
        drone_id=command.drone_id,
        connection_epoch=command.connection_epoch,
        seq=int(route["seq"]) + 2,
        issued_at=snapshot.now_ms,
        ttl_ms=1_000,
        operation=command.operation,
        args={
            "x_mm": int(round(float(command.parameters["x"]) * 1_000)),
            "y_mm": int(round(float(command.parameters["y"]) * 1_000)),
            "z_mm": int(round(float(command.parameters["z"]) * 1_000)),
            "speed_mm_s": int(round(float(command.parameters["speed"]) * 1_000)),
            "navigation_route_id": plan.intent_id,
        },
    )
    packets = {
        "route": route,
        "pose": control.initial_pose(1, session, snapshot.now_ms),
        "command": {**command_frame, "signature": sign_event(command_frame, KEY)},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(packets, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
