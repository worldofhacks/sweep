from __future__ import annotations

import json

import pytest

from relay.auth import sign_event
from relay.contracts import ContractError, command_event, parse_command


def navigation_command(route: object) -> dict:
    return command_event(
        t=1000,
        event_id="ground-route-event",
        session="demo",
        command_id="ground-route-command",
        intent_id="platform:destination-review",
        roster_version=4,
        drone_id=11,
        connection_epoch=2,
        seq=1,
        issued_at=1000,
        ttl_ms=1000,
        operation="ground_navigate",
        args={"route_id": "lobby-route", "navigation_route": route},
    )


def test_signed_ground_navigation_preserves_the_exact_route_document() -> None:
    route = json.dumps({"route_id": "lobby-route", "waypoints": [[1.25, 2.5]]})
    unsigned = navigation_command(route)
    signed = {**unsigned, "signature": sign_event(unsigned, b"route-contract-test-key")}
    received = parse_command(json.loads(json.dumps(signed)))
    assert received.operation.value == "ground_navigate"
    assert dict(received.args) == {"route_id": "lobby-route", "navigation_route": route}


@pytest.mark.parametrize("route", [None, {}, "", "x" * 32769, "é" * 16385])
def test_ground_navigation_refuses_nontext_or_oversized_route_documents(route: object) -> None:
    with pytest.raises((ValueError, ContractError)):
        navigation_command(route)
