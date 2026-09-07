from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("failure", ["", "root", "push", "running"])
def test_install_stages_as_shell_and_extracts_as_root_without_starting_motion(
    tmp_path: Path, failure: str
) -> None:
    calls = tmp_path / "calls.jsonl"
    adb = tmp_path / "adb"
    adb.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "script = sys.stdin.read() if args[2:] == ['shell', '-T', 'su', '0', 'sh'] else ''\n"
        "with open(os.environ['INSTALL_CALLS'], 'a') as stream:\n"
        "    stream.write(json.dumps([args, script]) + '\\n')\n"
        "failure = os.environ['INSTALL_FAILURE']\n"
        "if script == 'id -u\\n': print('2000' if failure == 'root' else '0')\n"
        "if failure == 'running' and 'node.pid' in script: sys.exit(1)\n"
        "if args[2] == 'push' and failure == 'push': sys.exit(1)\n"
        "if len(args) == 4 and args[3].startswith('mktemp'): "
        "print('/data/local/tmp/sweep-install.Ab12Cd34')\n"
    )
    adb.chmod(0o700)
    payload = tmp_path / "payload.tar"
    payload.write_bytes(b"payload")
    result = subprocess.run(
        ["sh", str(Path(__file__).with_name("install.sh")), "robot", str(payload)],
        env={
            **os.environ,
            "ADB": str(adb),
            "INSTALL_CALLS": str(calls),
            "INSTALL_FAILURE": failure,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    pushes = [args for args, _ in recorded if args[2] == "push"]
    extracts = [(args, script) for args, script in recorded if "toybox tar xf" in script]
    cleanup = [script for _, script in recorded if script.startswith("rm -rf ")]
    assert result.returncode == (0 if not failure else 1)
    assert not any("run.sh start" in script for _, script in recorded)
    if failure in {"root", "running"}:
        assert not pushes and not extracts and not cleanup
    else:
        assert pushes == [
            [
                "-s",
                "robot",
                "push",
                str(payload),
                "/data/local/tmp/sweep-install.Ab12Cd34/payload.tar",
            ]
        ]
        assert cleanup == ["rm -rf /data/local/tmp/sweep-install.Ab12Cd34\n"]
        if failure == "push":
            assert not extracts
        else:
            assert extracts[0][0] == ["-s", "robot", "shell", "-T", "su", "0", "sh"]
            assert (
                "toybox tar xf /data/local/tmp/sweep-install.Ab12Cd34/payload.tar" in extracts[0][1]
            )
