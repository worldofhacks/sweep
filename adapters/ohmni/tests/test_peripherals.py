"""Real bounded vendor requests against a recording shell, never physical hardware."""

from pathlib import Path

import pytest

from adapters.ohmni.peripherals import RobotPeripherals
from adapters.ohmni.tests.test_device import Shell, device
from nodekit.telemetry import device_telemetry_payload


def test_vendor_requests_use_exact_fixed_commands_and_unverified_readback():
    shell = Shell()
    controls = RobotPeripherals(shell)
    for args in (
        {"kind": "neck", "position": 512},
        {"kind": "lights", "h": 12, "s": 200, "v": 45},
        {"kind": "speech", "text": "Please keep the path clear."},
    ):
        assert "not reported" in controls.run(args, neck_allowed=True)
    assert shell.commands == [
        "neck_angle 512",
        "light_color 20 12 200 45",
        "say Please keep the path clear.",
    ]
    telemetry = controls.telemetry()
    assert telemetry["readback_verified"] is False
    assert telemetry["requested_neck_position"] == 512
    assert telemetry["requested_light_hsv"] == [12, 200, 45]
    assert device_telemetry_payload({"peripherals": telemetry}) == {"peripherals": telemetry}


def test_screen_is_plain_text_and_keeps_local_safety_page_controls():
    shell = Shell()
    controls = RobotPeripherals(shell)
    assert "STOP" in controls.run(
        {"kind": "screen", "text": "<b>Attention</b>"}, neck_allowed=False
    )
    assert controls.screen_message == "<b>Attention</b>"
    assert shell.commands == []
    controls.run({"kind": "screen", "text": ""}, neck_allowed=False)
    assert controls.screen_message == ""
    page = Path("adapters/ohmni/screen/index.html").read_text()
    assert "textContent=s.operator_message" in page
    assert 'id="stop"' in page


def test_neck_never_wakes_or_moves_a_locally_disabled_robot():
    robot = device()
    robot.disable()
    before = list(robot.drive_shell.commands)
    with pytest.raises(ValueError, match="local robot enabled"):
        robot.run_peripheral({"kind": "neck", "position": 512})
    assert robot.peripherals.shell.commands == []
    robot.run_peripheral({"kind": "lights", "h": 0, "s": 0, "v": 40})
    robot.run_peripheral({"kind": "screen", "text": "Waiting for spotter"})
    assert robot.drive_shell.commands == before
    assert not robot.enabled


@pytest.mark.parametrize("text", ["hello\nmanual_move 500 500", "hello\rsleep", "\x00", "\u2028"])
def test_vendor_speech_cannot_add_another_protocol_line(text):
    shell = Shell()
    controls = RobotPeripherals(shell)
    with pytest.raises(ValueError):
        controls.run({"kind": "speech", "text": text}, neck_allowed=True)
    assert shell.commands == []
