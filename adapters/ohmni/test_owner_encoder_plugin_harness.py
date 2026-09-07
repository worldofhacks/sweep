from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

NODE_DIR = "/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files"
FILES_DIR = "/data/data/com.ohmnilabs.telebot_rtc/files"
PLUGIN_DIR = f"{FILES_DIR}/plugins"
STAGE = "/data/local/tmp/sweep-encoder-plugin.Ab12Cd34"


def _write_command(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)


def _harness(tmp_path: Path, *, plugin_dir_exists: bool = True) -> tuple[dict[str, str], Path]:
    remote = tmp_path / "remote"
    source = remote / "files/assets/node-files/telebot_node.js"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        (
            Path(__file__).with_name("vendor") / "fixtures" / "telebot_node_reviewed_lf.js"
        ).read_bytes()
    )
    if plugin_dir_exists:
        (remote / "files/plugins").mkdir()
        (remote / "files/plugins").chmod(0o700)
    stage = remote / "local/tmp/sweep-encoder-plugin.Ab12Cd34"
    commands = tmp_path / "commands"
    commands.mkdir()
    _write_command(
        commands,
        "chown",
        "import sys\nraise SystemExit(0)\n",
    )
    _write_command(
        commands,
        "chcon",
        "import sys\nraise SystemExit(0)\n",
    )
    _write_command(
        commands,
        "stat",
        "import sys\n"
        "if sys.argv[1:3] == ['-c', '%a']:\n"
        "    print('600')\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[1:3] == ['-c', '%u:%g:%a']:\n"
        "    path = sys.argv[3]\n"
        "    if path.endswith('Ab12Cd34'): print('0:0:700')\n"
        "    elif path.endswith('sweep_encoder_plugin') or path.endswith('plugins'):\n"
        "        print('1000:1000:700')\n"
        "    else: print('1000:1000:600')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n",
    )
    _write_command(
        commands,
        "ls",
        "import sys\n"
        "if sys.argv[1] == '-Zd':\n"
        "    print('u:object_r:system_app_data_file:s0 ' + sys.argv[2])\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n",
    )
    adb = tmp_path / "adb"
    adb.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""\
            import os, pathlib, subprocess, sys
            args = sys.argv[1:]
            if args[2:] == ['get-state']:
                print('device')
                raise SystemExit(0)
            script = sys.stdin.read()
            if 'mktemp -d /data/local/tmp/sweep-encoder-plugin.XXXXXXXX' in script:
                pathlib.Path({str(stage)!r}).mkdir(parents=True, mode=0o700)
                print({STAGE!r})
                raise SystemExit(0)
            script = script.replace({FILES_DIR!r}, {str(remote / "files")!r})
            script = script.replace({STAGE!r}, {str(stage)!r})
            environment = dict(os.environ)
            environment['PATH'] = {str(commands)!r} + ':' + environment['PATH']
            completed = subprocess.run(['sh'], input=script, text=True, env=environment)
            raise SystemExit(completed.returncode)
            """
        )
    )
    adb.chmod(0o700)
    environment = {
        **os.environ,
        "ADB": str(adb),
    }
    return environment, remote


def _run(script: str, environment: dict[str, str]) -> None:
    completed = subprocess.run(
        ["sh", script, "robot"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_plugin_install_and_rollback_execute_against_a_temporary_remote_filesystem(
    tmp_path: Path,
) -> None:
    environment, remote = _harness(tmp_path)
    source = remote / "files/assets/node-files/telebot_node.js"
    source_before = source.read_bytes()
    plugin_source = Path(__file__).with_name("vendor") / "sweep_encoder_plugin.js"
    sampler_source = Path(__file__).with_name("vendor") / "sweep_encoder_plugin/sampler.js"
    install = Path(__file__).with_name("install_owner_encoder_plugin.sh")
    rollback = Path(__file__).with_name("rollback_owner_encoder_plugin.sh")

    _run(str(install), environment)

    plugin = remote / "files/plugins/sweep_encoder_plugin.js"
    sampler = remote / "files/plugins/sweep_encoder_plugin/sampler.js"
    manifest = remote / "files/plugins/sweep_encoder_plugin.install"
    assert source.read_bytes() == source_before
    assert plugin.read_bytes() == plugin_source.read_bytes()
    assert sampler.read_bytes() == sampler_source.read_bytes()
    assert stat.S_IMODE(plugin.stat().st_mode) == 0o600
    assert stat.S_IMODE(sampler.stat().st_mode) == 0o600
    assert stat.S_IMODE(sampler.parent.stat().st_mode) == 0o700
    assert (
        "vendor_source_sha=e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300"
        in manifest.read_text()
    )

    _run(str(rollback), environment)

    assert source.read_bytes() == source_before
    assert not plugin.exists()
    assert not sampler.exists()
    assert not sampler.parent.exists()
    assert not manifest.exists()
    assert sampler.parent.parent.exists()


def test_plugin_install_creates_and_rollback_removes_a_missing_plugin_directory(
    tmp_path: Path,
) -> None:
    environment, remote = _harness(tmp_path, plugin_dir_exists=False)
    source = remote / "files/assets/node-files/telebot_node.js"
    source_before = source.read_bytes()
    install = Path(__file__).with_name("install_owner_encoder_plugin.sh")
    rollback = Path(__file__).with_name("rollback_owner_encoder_plugin.sh")

    _run(str(install), environment)

    plugin_dir = remote / "files/plugins"
    manifest = plugin_dir / "sweep_encoder_plugin.install"
    assert plugin_dir.is_dir()
    assert stat.S_IMODE(plugin_dir.stat().st_mode) == 0o700
    assert "plugin_dir_created=1" in manifest.read_text()
    assert source.read_bytes() == source_before

    _run(str(rollback), environment)

    assert source.read_bytes() == source_before
    assert not plugin_dir.exists()


def test_plugin_layout_matches_the_vendor_flat_loader_and_reviewed_sampler() -> None:
    vendor_source = (
        Path(__file__).with_name("vendor") / "fixtures" / "telebot_node_reviewed_lf.js"
    ).read_text()
    plugin = Path(__file__).with_name("vendor") / "sweep_encoder_plugin.js"
    sampler = Path(__file__).with_name("vendor") / "sweep_encoder_plugin/sampler.js"
    reviewed_sampler = Path(__file__).with_name("vendor") / "sweep_paired_encoder_sampler.js"

    assert 'if (!f.endsWith(".js")) return;' in vendor_source
    assert "sweep_encoder_plugin/sampler.js" not in plugin.read_text()
    assert sampler.read_bytes() == reviewed_sampler.read_bytes()
