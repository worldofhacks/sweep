from __future__ import annotations

from collections import deque

from adapters.dji_mini3.remote import CommandRequest
from planner.models import CommandOperation, LifecycleStatus, RefusalReason
from relay.contracts import AdapterAcknowledgement
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import make_intent

from .dispatcher import GroundCommandDispatcher


class _StopLink:
    def __init__(self, failed_drone: int | None = None) -> None:
        self.failed_drone = failed_drone
        self.events: list[tuple[str, str]] = []
        self.requests: list[CommandRequest] = []
        self.pending: dict[str, deque[AdapterAcknowledgement]] = {}

    def connection_epoch(self, drone_id: int) -> int | None:
        return 4 if drone_id in {9, 10} else None

    def send(self, request: CommandRequest) -> None:
        self.events.append(("send", request.command_id))
        self.requests.append(request)
        status = "failed" if request.drone_id == self.failed_drone else "completed"
        self.pending[request.command_id] = deque(
            [
                AdapterAcknowledgement(
                    1,
                    1,
                    "acknowledgement",
                    f"ack-{request.command_id}",
                    "test-session",
                    request.intent_id,
                    request.command_id,
                    WireLifecycleStatus(status),
                    request.drone_id,
                    request.connection_epoch,
                    request.roster_version,
                    RefusalReason.ADAPTER_FAILURE.value if status == "failed" else None,
                    "forced stop failure" if status == "failed" else None,
                )
            ]
        )

    def await_acknowledgement(
        self, command_id: str, *, timeout_ms: int
    ) -> AdapterAcknowledgement | None:
        self.events.append(("await", command_id))
        pending = self.pending[command_id]
        return pending.popleft() if pending else None


def _state() -> dict[str, object]:
    return {
        "roster_version": 7,
        "drones": [
            {"drone_id": 9, "node_type": "ground", "connection_epoch": 4},
            {"drone_id": 10, "node_type": "ground", "connection_epoch": 4},
        ],
    }


def _dispatcher(link: _StopLink) -> GroundCommandDispatcher:
    counter = iter(("stop-9", "stop-10"))
    return GroundCommandDispatcher(
        link,
        acknowledgement_timeout_ms=10,
        command_deadline_ms=10,
        command_ids=lambda: next(counter),
        monotonic=lambda: 0,
    )


def test_global_estop_fans_out_before_waiting_and_aggregates_terminal_acknowledgements() -> None:
    link = _StopLink()
    result = _dispatcher(link).dispatch_stop(
        make_intent(IntentName.ESTOP, selection=(), intent_id="stop-all"), _state()
    )

    assert [(request.drone_id, request.operation) for request in link.requests] == [
        (9, CommandOperation.ESTOP),
        (10, CommandOperation.ESTOP),
    ]
    assert [event[0] for event in link.events[:2]] == ["send", "send"]
    assert result.status is LifecycleStatus.COMPLETED
    assert result.plan is not None
    assert {acknowledgement.drone_id for acknowledgement in result.acknowledgements} == {9, 10}


def test_stop_failure_is_terminal_and_cannot_be_reported_as_accepted() -> None:
    link = _StopLink(failed_drone=10)
    result = _dispatcher(link).dispatch_stop(
        make_intent(IntentName.HOLD, selection=(9, 10), intent_id="hold-ground"), _state()
    )

    assert result.status is LifecycleStatus.FAILED
    assert result.refusal is not None
    assert result.refusal.drone_id == 10
    assert result.refusal.reason is RefusalReason.ADAPTER_FAILURE
    assert result.degraded_aircraft == (10,)
