"""Bounded vendor peripheral requests; no arbitrary bot-shell command interface.

Verified source: telebot 4.1.4.4 bot_shell_neck.js cmd_neck_angle,
bot_shell_core.js cmd_light_color and bot_shell_speech.js cmd_say.
The vendor shell does not acknowledge physical completion. Telemetry records
requested state, explicitly distinguished from measured position/output.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from nodekit.peripherals import PERIPHERAL_KINDS, peripheral_arguments

from .botshell import BotShell


class RobotPeripherals:
    kinds = PERIPHERAL_KINDS

    def __init__(self, shell: BotShell) -> None:
        self.shell = shell
        self._lock = threading.Lock()
        self._requested: dict[str, object] = {}
        self._screen_message = ""

    @property
    def screen_message(self) -> str:
        with self._lock:
            return self._screen_message

    def run(self, raw: Mapping[str, object], *, neck_allowed: bool) -> str:
        args = peripheral_arguments(raw)
        kind = args["kind"]
        with self._lock:
            if kind == "neck":
                if not neck_allowed:
                    raise ValueError(
                        "neck requires the local robot enabled, undocked and a spotter present"
                    )
                # Use the vendor control model; never auto-wake or change torque/firmware.
                self.shell.command(f"neck_angle {args['position']}")
                self._requested["neck_position"] = args["position"]
            elif kind == "lights":
                self.shell.command(f"light_color 20 {args['h']} {args['s']} {args['v']}")
                self._requested["light_hsv"] = [args["h"], args["s"], args["v"]]
            elif kind == "speech":
                # cmd_say joins these tokens into JSON text; this is not an OS shell.
                self.shell.command(f"say {args['text']}")
                self._requested["speech_text"] = args["text"]
            elif kind == "screen":
                self._screen_message = str(args["text"])
                self._requested["screen_text"] = args["text"]
                return (
                    "Local safety-page message updated; STOP and spotter controls remain visible."
                )
        return (
            "Submitted to the vendor API; physical completion is not reported. "
            "Neck movement requires an already-awake local neck."
            if kind == "neck"
            else "Submitted to the vendor API; physical output is not reported."
        )

    def telemetry(self) -> dict[str, object]:
        with self._lock:
            return {
                **{f"requested_{key}": value for key, value in self._requested.items()},
                "readback_verified": False,
                "vendor_api": "telebot_bot_shell",
                "neck_requires_local_enable": True,
                "screen_message": self._screen_message,
            }

    def close(self) -> None:
        self.shell.close()
