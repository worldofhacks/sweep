from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

VENDOR_SOURCE = "/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files/telebot_node.js"
VENDOR_SOURCE_SHA = "e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300"


def _fake_adb(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "calls.jsonl"
    adb = tmp_path / "adb"
    adb.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "script = sys.stdin.read() if args[2:] == ['shell', '-T', 'su', '0', 'sh'] else ''\n"
        "with open(os.environ['PLUGIN_INSTALL_CALLS'], 'a') as stream:\n"
        "    stream.write(json.dumps([args, script]) + '\\n')\n"
        "if args[2:] == ['get-state']:\n"
        "    print('device')\n"
        "elif 'mktemp -d /data/local/tmp/sweep-encoder-plugin.XXXXXXXX' in script:\n"
        "    print('/data/local/tmp/sweep-encoder-plugin.Ab12Cd34')\n"
        f"elif 'sha256sum {VENDOR_SOURCE}' in script:\n"
        "    print(os.environ.get('PLUGIN_VENDOR_SHA', '"
        + VENDOR_SOURCE_SHA
        + "') + '  ' + '"
        + VENDOR_SOURCE
        + "')\n"
    )
    adb.chmod(0o700)
    return adb, calls


def _run(
    tmp_path: Path, script: str, *, vendor_sha: str | None = None
) -> subprocess.CompletedProcess[str]:
    adb, calls = _fake_adb(tmp_path)
    environment = {
        **os.environ,
        "ADB": str(adb),
        "PLUGIN_INSTALL_CALLS": str(calls),
    }
    if vendor_sha is not None:
        environment["PLUGIN_VENDOR_SHA"] = vendor_sha
    return subprocess.run(
        ["sh", script, "robot"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=10,
    )


def _calls(tmp_path: Path) -> list[tuple[list[str], str]]:
    return [tuple(json.loads(line)) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def test_plugin_installer_preserves_reviewed_vendor_source_and_stages_private_module(
    tmp_path: Path,
) -> None:
    script = str(Path(__file__).with_name("install_owner_encoder_plugin.sh"))

    completed = _run(tmp_path, script)

    assert completed.returncode == 0, completed.stderr
    scripts = [script for _, script in _calls(tmp_path)]
    install = next(
        script
        for script in scripts
        if "target_sampler=/data/data/com.ohmnilabs.telebot_rtc/files/plugins/"
        "sweep_encoder_plugin/sampler.js" in script
    )
    assert f"sha256sum {VENDOR_SOURCE}" in scripts[1]
    assert f"= {VENDOR_SOURCE_SHA} ]" in install
    assert "sweep_encoder_plugin/sampler.js" in install
    assert "[ ! -e $target ]" in install
    assert "[ ! -e $private_dir ]" in install
    assert "[ ! -e $manifest ]" in install
    assert "[ ! -L ${plugin_dir} ]" in install
    assert "plugin_dir_created=" in install
    assert "stat -c '%u:%g:%a' $plugin_dir" in install
    assert "cp -p $source" not in install
    assert (
        "mv /data/local/tmp/sweep-encoder-plugin.Ab12Cd34/sweep_encoder_plugin.js $target"
        in install
    )
    assert "mv /data/local/tmp/sweep-encoder-plugin.Ab12Cd34/sampler.js $target_sampler" in install
    assert "mv $source" not in install
    assert "chown 1000:1000 $target $target_sampler $manifest" in install
    assert not any(args[2] == "push" for args, _ in _calls(tmp_path))


def test_plugin_installer_refuses_a_changed_or_patched_vendor_source(tmp_path: Path) -> None:
    script = str(Path(__file__).with_name("install_owner_encoder_plugin.sh"))

    completed = _run(tmp_path, script, vendor_sha=hashlib.sha256(b"changed").hexdigest())

    assert completed.returncode == 1
    assert "reviewed original" in completed.stderr
    assert len(_calls(tmp_path)) == 2


def test_plugin_rollback_is_hash_guarded_and_leaves_vendor_source_in_place() -> None:
    script = Path(__file__).with_name("rollback_owner_encoder_plugin.sh").read_text()

    assert f'source_sha="{VENDOR_SOURCE_SHA}"' in script
    assert r"sha256sum \$source" in script
    assert "grep -Fx 'vendor_source_sha=" in script
    assert r"rmdir \$private_dir" in script
    assert r"rm \$target \$target_sampler \$manifest" in script
    assert "mv $source" not in script
    assert "telebot_node.js.sweep-owner-encoder" not in script
