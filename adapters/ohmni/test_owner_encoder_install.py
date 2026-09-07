from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REFERENCE_SHA = "f463feaab912999d3b4133fea049925ed95a6e32ee82856fb7ffa273cbe2ca3e"
NODE_DIR = "/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files"


def _tools(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    adb = bin_dir / "adb"
    adb.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "target = '" + NODE_DIR + "/telebot_node.js'\n"
        "script = sys.stdin.read() if args[2:] == ['shell', '-T', 'su', '0', 'sh'] else ''\n"
        "with open(os.environ['OWNER_INSTALL_CALLS'], 'a') as stream:\n"
        "    stream.write(json.dumps([args, script]) + '\\n')\n"
        "if args[2] == 'pull': raise SystemExit('unprivileged protected read')\n"
        "if args[2:] == ['exec-out', 'su', '0', 'cat', target]:\n"
        "    sys.stdout.buffer.write(b'original')\n"
        "elif args[2:] == ['exec-out', 'su', '0', 'sha256sum', target]:\n"
        "    print('" + REFERENCE_SHA + "  telebot_node.js')\n"
        "if args[2] == 'shell' and args[-1].startswith('mktemp'):\n"
        "    print('/data/local/tmp/sweep-owner-patch.Ab12Cd34')\n"
    )
    python = bin_dir / "python3"
    python.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[-1]).write_bytes(b'patched')\n"
    )
    adb.chmod(0o700)
    python.chmod(0o700)
    return bin_dir, calls


def _run(script: str, tmp_path: Path) -> list[tuple[list[str], str]]:
    bin_dir, calls = _tools(tmp_path)
    subprocess.run(
        ["sh", script, "robot", NODE_DIR],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "OWNER_INSTALL_CALLS": str(calls),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return [tuple(json.loads(line)) for line in calls.read_text().splitlines()]


def test_owner_install_rechecks_content_and_restores_vendor_metadata(tmp_path: Path) -> None:
    calls = _run(str(Path(__file__).with_name("install_owner_encoder_patch.sh")), tmp_path)
    scripts = [script for _, script in calls]
    install = next(script for script in scripts if "cp -p $target $backup" in script)
    assert f"= {REFERENCE_SHA} ]" in install
    assert any(
        args[2:] == ["exec-out", "su", "0", "cat", f"{NODE_DIR}/telebot_node.js"]
        for args, _ in calls
    )
    assert any(
        args[2:] == ["exec-out", "su", "0", "sha256sum", f"{NODE_DIR}/telebot_node.js"]
        for args, _ in calls
    )
    assert not any(args[2] == "pull" for args, _ in calls)
    assert "chown 1000:1000 $target $module" in install
    assert "chmod 600 $target $module" in install
    assert "chcon u:object_r:system_app_data_file:s0 $target $module" in install
    assert "stat -c '%u:%g:%a' $target" in install
    assert "ls -Zd $target" in install
    assert not any("run.sh start" in script for script in scripts)


def test_owner_rollback_verifies_backup_and_restores_vendor_metadata(tmp_path: Path) -> None:
    calls = _run(str(Path(__file__).with_name("rollback_owner_encoder_patch.sh")), tmp_path)
    rollback = next(
        script for _, script in calls if "telebot_node.js.sweep-owner-encoder.disabled" in script
    )
    assert "sha256sum $backup" in rollback
    assert f"= {REFERENCE_SHA} ]" in rollback
    assert "chown 1000:1000 $target" in rollback
    assert "chmod 600 $target" in rollback
    assert "chcon u:object_r:system_app_data_file:s0 $target" in rollback


def test_owner_rollback_pins_current_sampler_module() -> None:
    module = Path(__file__).with_name("vendor") / "sweep_paired_encoder_sampler.js"
    module_sha = hashlib.sha256(module.read_bytes()).hexdigest()
    rollback = Path(__file__).with_name("rollback_owner_encoder_patch.sh").read_text()
    assert f'module_sha="{module_sha}"' in rollback
