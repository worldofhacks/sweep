from __future__ import annotations

from collections import deque
from collections.abc import Mapping

import pytest

from adapters.dispatch import AdapterDispatcher
from adapters.dji_mini3.remote import CommandRequest, RemoteBridgeAdapter
from adapters.protocols import (
    AdapterError,
    AdapterTimeout,
    CameraResultStatus,
    CameraStateCode,
    CapturePattern,
)
from arbiter.safety import SafetyArbiter
from planner.controller import AutonomyController
from planner.models import (
    Command,
    CommandOperation,
    LifecycleStatus,
    Plan,
    RefusalReason,
)
from planner.planner import DeterministicPlanner
from relay.contracts import AdapterAcknowledgement as WireAcknowledgement
from relay.contracts import (
    CapabilitiesFrame,
    MediaFileRecord,
    WireIntrinsics,
    WirePose,
)
from relay.contracts import LifecycleStatus as WireLifecycleStatus
from relay.intent_v1 import IntentName
from tests.autonomy_fixtures import (
    make_intent,
    make_snapshot,
    planning_config,
    safety_config,
)

Script = list[tuple[str, str | None, str | None]]
TIMEOUT_MS = 50


class ScriptedLink:
    """Fake node link that answers each sent command from a per-operation script."""

    def __init__(
        self,
        *,
        epochs: Mapping[int, int],
        scripts: Mapping[CommandOperation, Script] | None = None,
        capabilities: Mapping[int, CapabilitiesFrame] | None = None,
        media: Mapping[str, tuple[MediaFileRecord, ...]] | None = None,
    ) -> None:
        self.epochs = dict(epochs)
        self.scripts = dict(scripts or {})
        self._capabilities = dict(capabilities or {})
        self._media = dict(media or {})
        self.sent: list[CommandRequest] = []
        self._pending: dict[str, deque[WireAcknowledgement]] = {}
        self._clock = 100_000

    def connection_epoch(self, drone_id: int) -> int | None:
        return self.epochs.get(drone_id)

    def send(self, request: CommandRequest) -> None:
        self.sent.append(request)
        script = self.scripts.get(request.operation, [("completed", None, None)])
        acknowledgements: deque[WireAcknowledgement] = deque()
        for status, reason, detail in script:
            self._clock += 1
            acknowledgements.append(
                WireAcknowledgement(
                    1,
                    self._clock,
                    "acknowledgement",
                    f"ack-{request.command_id}-{status}",
                    "test-session",
                    request.intent_id,
                    request.command_id,
                    WireLifecycleStatus(status),
                    request.drone_id,
                    request.connection_epoch,
                    request.roster_version,
                    reason,
                    detail,
                )
            )
        self._pending[request.command_id] = acknowledgements

    def await_acknowledgement(
        self, command_id: str, *, timeout_ms: int
    ) -> WireAcknowledgement | None:
        pending = self._pending.get(command_id)
        if not pending:
            return None
        return pending.popleft()

    def camera_capabilities(self, drone_id: int) -> CapabilitiesFrame | None:
        return self._capabilities.get(drone_id)

    def media_files(self, drone_id: int, capture_id: str) -> tuple[MediaFileRecord, ...]:
        return tuple(
            record
            for record in self._media.get(capture_id, ())
            if record.drone_id == drone_id and record.connection_epoch == self.epochs[drone_id]
        )


def _adapter(link: ScriptedLink, *, epochs: Mapping[int, int] | None = None) -> RemoteBridgeAdapter:
    return RemoteBridgeAdapter(
        link,
        epochs=epochs if epochs is not None else link.epochs,
        acknowledgement_timeout_ms=TIMEOUT_MS,
        command_ids=iter(f"wire-{index}" for index in range(1, 100)).__next__,
    )


def _dispatcher(adapter: RemoteBridgeAdapter) -> AdapterDispatcher:
    return AdapterDispatcher(flight=adapter, camera=adapter, arbiter=SafetyArbiter(safety_config()))


def _hover_command(snapshot: object, *, connection_epoch: int = 1) -> Command:
    return Command(
        command_id="command-hover",
        intent_id="intent-1",
        roster_version=snapshot.roster_version,  # type: ignore[attr-defined]
        drone_id=1,
        connection_epoch=connection_epoch,
        operation=CommandOperation.HOVER,
    )


def _capabilities_frame(drone_id: int = 1, connection_epoch: int = 1) -> CapabilitiesFrame:
    return CapabilitiesFrame(
        1,
        100_000,
        "capabilities",
        "capabilities-1",
        "test-session",
        drone_id,
        connection_epoch,
        ("pano_360",),
        True,
        -90.0,
        30.0,
        66.0,
        50_000_000,
        True,
        "DJI Mini 3",
        "01.00.05.00",
        "04.16.05.00",
        "fake-node",
        "14",
        "5.18.0",
        None,
    )


def _panorama_record(capture_id: str, *, timestamp_ms: int = 100_100) -> MediaFileRecord:
    return MediaFileRecord(
        capture_id,
        f"{capture_id}-pano-360",
        timestamp_ms,
        1,
        1,
        WirePose(0.0, 0.0, 1.0),
        0.0,
        0.0,
        WireIntrinsics(4_096, 2_048, 360.0, "equirectangular"),
        "a" * 64,
        f"node://media/1/{capture_id}-pano-360",
        "completed",
    )


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ([("accepted", None, None)], LifecycleStatus.ACCEPTED),
        ([("accepted", None, None), ("executing", None, None)], LifecycleStatus.EXECUTING),
        (
            [("accepted", None, None), ("executing", None, None), ("completed", None, None)],
            LifecycleStatus.COMPLETED,
        ),
    ],
)
def test_hover_returns_the_latest_acknowledgement_until_a_terminal_one(
    script: Script, expected: LifecycleStatus
) -> None:
    snapshot = make_snapshot(1, selection=(1,))
    link = ScriptedLink(epochs={1: 1}, scripts={CommandOperation.HOVER: script})
    adapter = _adapter(link)

    with adapter.for_intent("intent-1", snapshot.roster_version):
        (acknowledgement,) = adapter.hover([1])

    assert acknowledgement.status is expected
    assert acknowledgement.operation is CommandOperation.HOVER
    assert acknowledgement.connection_epoch == 1
    assert acknowledgement.detail == ""
    assert [request.operation for request in link.sent] == [CommandOperation.HOVER]
    assert link.sent[0].intent_id == "intent-1"
    assert link.sent[0].roster_version == snapshot.roster_version
    assert dict(link.sent[0].args) == {}
    validated = _dispatcher(adapter).validate_acknowledgement(
        _hover_command(snapshot), acknowledgement, snapshot
    )
    assert validated.status is expected
    assert validated.reason is None


def test_failed_acknowledgement_carries_the_node_reason_into_detail() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    link = ScriptedLink(
        epochs={1: 1},
        scripts={
            CommandOperation.HOVER: [
                ("accepted", None, None),
                ("failed", "authority_lost", "physical RC took over"),
            ]
        },
    )
    adapter = _adapter(link)

    with adapter.for_intent("intent-1", snapshot.roster_version):
        (acknowledgement,) = adapter.hover([1])

    assert acknowledgement.status is LifecycleStatus.FAILED
    assert acknowledgement.detail == "authority_lost: physical RC took over"
    validated = _dispatcher(adapter).validate_acknowledgement(
        _hover_command(snapshot), acknowledgement, snapshot
    )
    assert validated.status is LifecycleStatus.FAILED
    assert validated.reason is RefusalReason.ADAPTER_FAILURE
    assert validated.detail == "authority_lost: physical RC took over"


def test_silence_raises_adapter_timeout_without_a_resend() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    link = ScriptedLink(epochs={1: 1}, scripts={CommandOperation.HOVER: []})
    adapter = _adapter(link)

    with adapter.for_intent("intent-1", snapshot.roster_version):
        with pytest.raises(AdapterTimeout) as timeout:
            adapter.hover([1])

    assert timeout.value.drone_id == 1
    assert timeout.value.operation is CommandOperation.HOVER
    assert len(link.sent) == 1


def test_stale_connection_epoch_is_rejected_before_send() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    link = ScriptedLink(epochs={1: 2})
    adapter = _adapter(link, epochs={1: 1})

    with adapter.for_intent("intent-1", snapshot.roster_version):
        (acknowledgement,) = adapter.hover([1])

    assert link.sent == []
    assert acknowledgement.status is LifecycleStatus.FAILED
    assert acknowledgement.connection_epoch == 2
    validated = _dispatcher(adapter).validate_acknowledgement(
        _hover_command(snapshot), acknowledgement, snapshot
    )
    assert validated.status is LifecycleStatus.REFUSED
    assert validated.reason is RefusalReason.STALE_CONNECTION_EPOCH


def test_node_echoing_out_of_order_command_is_terminal_and_never_resent() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    link = ScriptedLink(
        epochs={1: 1},
        scripts={CommandOperation.HOVER: [("failed", "out_of_order_command", "seq 3 after seq 4")]},
    )
    adapter = _adapter(link)

    with adapter.for_intent("intent-1", snapshot.roster_version):
        (acknowledgement,) = adapter.hover([1])
        later = adapter.hover([1])

    assert acknowledgement.status is LifecycleStatus.FAILED
    assert acknowledgement.detail.startswith("out_of_order_command")
    assert later[0].status is LifecycleStatus.FAILED
    assert [request.command_id for request in link.sent] == ["wire-1", "wire-2"]


def test_flight_arguments_are_sent_in_integer_units() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    link = ScriptedLink(epochs={1: 1})
    adapter = _adapter(link)

    with adapter.for_intent("intent-1", snapshot.roster_version):
        adapter.takeoff([1], 1.2)
        adapter.goto(1, 1.25, -0.4, 1.0, 0.5)
        adapter.rotate_to(1, 90.0, 30.0)
        adapter.set_gimbal_pitch(1, -15.0)
        adapter.land([1])
        adapter.estop()

    assert [dict(request.args) for request in link.sent] == [
        {"z_mm": 1_200},
        {"x_mm": 1_250, "y_mm": -400, "z_mm": 1_000, "speed_mm_s": 500},
        {"yaw_mdeg": 90_000, "speed_mdeg_s": 30_000},
        {"pitch_mdeg": -15_000},
        {},
        {},
    ]
    assert [request.operation for request in link.sent] == [
        CommandOperation.TAKEOFF,
        CommandOperation.GOTO,
        CommandOperation.ROTATE_TO,
        CommandOperation.SET_GIMBAL_PITCH,
        CommandOperation.LAND,
        CommandOperation.ESTOP,
    ]
    assert list(adapter.telemetry()) == []


def test_commands_require_a_bound_intent_context() -> None:
    link = ScriptedLink(epochs={1: 1})
    adapter = _adapter(link)

    with pytest.raises(AdapterError, match="intent"):
        adapter.hover([1])
    assert link.sent == []


def test_camera_readiness_maps_node_outcomes_to_camera_state() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    ready_link = ScriptedLink(epochs={1: 1})
    unsupported_link = ScriptedLink(
        epochs={1: 1},
        scripts={
            CommandOperation.CAMERA_READY: [("failed", "camera_unsupported", "no photo mode")]
        },
    )
    busy_link = ScriptedLink(
        epochs={1: 1},
        scripts={CommandOperation.CAMERA_READY: [("failed", "camera_not_ready", "busy")]},
    )
    states = []
    for link in (ready_link, unsupported_link, busy_link):
        adapter = _adapter(link)
        with adapter.for_intent("intent-1", snapshot.roster_version):
            states.append(adapter.ready(1))

    assert [state.state for state in states] == [
        CameraStateCode.READY,
        CameraStateCode.UNSUPPORTED,
        CameraStateCode.ERROR,
    ]
    assert states[2].detail == "camera_not_ready: busy"


def test_capabilities_require_a_capabilities_frame_after_completion() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    silent = _adapter(ScriptedLink(epochs={1: 1}))
    reported = _adapter(ScriptedLink(epochs={1: 1}, capabilities={1: _capabilities_frame()}))

    with silent.for_intent("intent-1", snapshot.roster_version):
        with pytest.raises(AdapterError, match="capabilities"):
            silent.capabilities(1)
    with reported.for_intent("intent-1", snapshot.roster_version):
        capabilities = reported.capabilities(1)

    assert capabilities.supports(CapturePattern.PANO_360)
    assert capabilities.connection_epoch == 1
    assert capabilities.horizontal_fov_deg == 66.0


def test_translate_plan_dispatches_through_the_remote_adapter() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    plan = DeterministicPlanner(planning_config()).plan(
        make_intent(IntentName.TRANSLATE, selection=(1,), args={"dx": 1, "dy": 0}),
        snapshot,
    )
    assert isinstance(plan, Plan)
    link = ScriptedLink(epochs={1: 1})
    adapter = _adapter(link)

    result = _dispatcher(adapter).dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.COMPLETED
    assert [request.operation for request in link.sent] == [CommandOperation.GOTO]
    assert link.sent[0].intent_id == plan.intent_id
    assert link.sent[0].roster_version == plan.roster_version
    assert link.sent[0].connection_epoch == 1


def test_autonomy_controller_drives_the_remote_adapter_without_a_caller_scope() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    intent = make_intent(IntentName.HOLD, selection=(1,))
    link = ScriptedLink(epochs={1: 1})
    adapter = _adapter(link)
    arbiter = SafetyArbiter(safety_config())
    controller = AutonomyController(
        planner=DeterministicPlanner(planning_config()),
        arbiter=arbiter,
        dispatcher=AdapterDispatcher(flight=adapter, camera=adapter, arbiter=arbiter),
    )

    result = controller.execute(intent, snapshot)

    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert [
        (request.operation, request.intent_id, request.roster_version) for request in link.sent
    ] == [(CommandOperation.HOVER, intent.intent_id, snapshot.roster_version)]
    assert [ack.status for ack in result.acknowledgements] == [LifecycleStatus.COMPLETED]


def test_dispatcher_scopes_safety_holds_and_estop_to_their_plan() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    plan = DeterministicPlanner(planning_config()).plan(
        make_intent(IntentName.TRANSLATE, selection=(1,), args={"dx": 1, "dy": 0}),
        snapshot,
    )
    assert isinstance(plan, Plan)
    failing = ScriptedLink(
        epochs={1: 1},
        scripts={CommandOperation.GOTO: [("failed", "authority_lost", "physical RC took over")]},
    )
    estop_plan = DeterministicPlanner(planning_config()).plan(
        make_intent(IntentName.ESTOP, selection=(1,)), snapshot
    )
    assert isinstance(estop_plan, Plan)
    stopping = ScriptedLink(epochs={1: 1})

    failed = _dispatcher(_adapter(failing)).dispatch(plan, snapshot)
    stopped = _dispatcher(_adapter(stopping)).dispatch(estop_plan, snapshot)

    assert failed.status is LifecycleStatus.FAILED
    assert [
        (request.operation, request.intent_id, request.roster_version) for request in failing.sent
    ] == [
        (CommandOperation.GOTO, plan.intent_id, plan.roster_version),
        (CommandOperation.HOVER, plan.intent_id, snapshot.roster_version),
    ]
    assert stopped.status is LifecycleStatus.COMPLETED
    assert [(request.operation, request.intent_id) for request in stopping.sent] == [
        (CommandOperation.ESTOP, estop_plan.intent_id)
    ]


def test_panorama_capture_round_trips_media_through_the_remote_adapter() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    intent = make_intent(
        IntentName.CAPTURE_ROOM,
        selection=(1,),
        args={"room_id": "room-a", "capture_id": "capture-a", "pattern": "pano_360"},
        confirm=True,
    )
    plan = DeterministicPlanner(planning_config()).plan(intent, snapshot)
    assert isinstance(plan, Plan)
    link = ScriptedLink(
        epochs={1: 1},
        capabilities={1: _capabilities_frame()},
        media={"capture-a": (_panorama_record("capture-a"),)},
    )
    adapter = _adapter(link)

    result = _dispatcher(adapter).dispatch(plan, snapshot)

    assert result.status is LifecycleStatus.COMPLETED, result.refusal
    assert {request.intent_id for request in link.sent} == {plan.intent_id}
    bundle = result.capture_bundle
    assert bundle is not None
    assert bundle.status is CameraResultStatus.COMPLETED
    assert bundle.pattern is CapturePattern.PANO_360
    assert [media.file_id for media in bundle.media] == ["capture-a-pano-360"]
    assert bundle.media[0].checksum_sha256 == "a" * 64
    assert [request.operation for request in link.sent] == [
        CommandOperation.CAMERA_CAPABILITIES,
        CommandOperation.SET_GIMBAL_PITCH,
        CommandOperation.CAMERA_READY,
        CommandOperation.CAPTURE_PANORAMA,
        CommandOperation.RETRIEVE_MEDIA,
    ]
    assert dict(link.sent[3].args) == {"capture_id": "capture-a"}
    assert dict(link.sent[4].args) == {"file_id": "capture-a-pano-360"}


def test_capture_without_a_media_file_and_unknown_retrieval_fail_closed() -> None:
    snapshot = make_snapshot(1, selection=(1,))
    link = ScriptedLink(epochs={1: 1})
    adapter = _adapter(link)

    with adapter.for_intent("intent-1", snapshot.roster_version):
        with pytest.raises(AdapterError, match="media_file"):
            adapter.capture_photo(1, "capture-b")
        unknown = adapter.retrieve(1, "never-captured")

    assert unknown.status is CameraResultStatus.FAILED
    assert unknown.reason is RefusalReason.DOWNLOAD_FAILURE
    assert [request.operation for request in link.sent] == [CommandOperation.CAPTURE_PHOTO]
