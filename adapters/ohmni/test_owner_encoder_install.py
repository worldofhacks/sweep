from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CRLF_REFERENCE_SHA = "f463feaab912999d3b4133fea049925ed95a6e32ee82856fb7ffa273cbe2ca3e"
LF_REFERENCE_SHA = "e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300"
CRLF_PATCHED_SHA = "0394a830141bf8ce4343944b768de17887531f3c1d89e3521216b5f7ca5b82ea"
LF_PATCHED_SHA = "ee0a0665dc1a5931960d97032405cb4e7baf731d0cc6b08738a4d33302a5bf42"
NODE_DIR = "/data/data/com.ohmnilabs.telebot_rtc/files/assets/node-files"


def _tools(
    tmp_path: Path,
    *,
    source: bytes = b"original",
    root_stage: Path | None = None,
    fake_patcher: bool = True,
) -> tuple[Path, Path]:
    calls = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    adb = bin_dir / "adb"
    adb.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""\
            import base64, json, os, subprocess, sys
            args = sys.argv[1:]
            target = {NODE_DIR + "/telebot_node.js"!r}
            script = sys.stdin.read() if args[2:] == ['shell', '-T', 'su', '0', 'sh'] else ''
            with open(os.environ['OWNER_INSTALL_CALLS'], 'a') as stream:
                stream.write(json.dumps([args, script]) + '\\n')
            if len(args) > 2 and args[2] == 'push':
                sys.stderr.write('remote could not create file: Permission denied\\n')
                raise SystemExit(1)
            if 'base64 ' + target in script:
                sys.stdout.write(base64.b64encode({source!r}).decode() + '\\n')
            elif 'sha256sum ' + target in script:
                remote_sha = os.environ.get(
                    'OWNER_REMOTE_SHA', {hashlib.sha256(source).hexdigest()!r}
                )
                print(remote_sha + '  ' + target)
            elif 'mktemp -d /data/local/tmp/sweep-owner-patch.XXXXXXXX' in script:
                root_stage = os.environ.get('OWNER_ROOT_STAGE')
                if root_stage:
                    os.mkdir(root_stage, 0o700)
                print('/data/local/tmp/sweep-owner-patch.Ab12Cd34')
            elif 'base64 -d > /data/local/tmp/sweep-owner-patch.Ab12Cd34/' in script:
                root_stage = os.environ.get('OWNER_ROOT_STAGE')
                if root_stage:
                    stage = '/data/local/tmp/sweep-owner-patch.Ab12Cd34'
                    subprocess.run(
                        ['sh'],
                        input=script.replace(stage, root_stage),
                        text=True,
                        check=True,
                    )
            """
        )
    )
    adb.chmod(0o700)
    if fake_patcher:
        python = bin_dir / "python3"
        python.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[-1]).write_bytes(b'patched')\n"
        )
        python.chmod(0o700)
    return bin_dir, calls


def _run(
    script: str,
    tmp_path: Path,
    *,
    remote_sha: str | None = None,
    source: bytes = b"original",
    root_stage: Path | None = None,
    fake_patcher: bool = True,
) -> list[tuple[list[str], str]]:
    bin_dir, calls = _tools(
        tmp_path, source=source, root_stage=root_stage, fake_patcher=fake_patcher
    )
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OWNER_INSTALL_CALLS": str(calls),
    }
    if remote_sha is not None:
        environment["OWNER_REMOTE_SHA"] = remote_sha
    if root_stage is not None:
        environment["OWNER_ROOT_STAGE"] = str(root_stage)
    subprocess.run(
        ["sh", script, "robot", NODE_DIR],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return [tuple(json.loads(line)) for line in calls.read_text().splitlines()]


def test_root_stage_rejects_adb_push_but_installer_transfers_exact_bytes_through_root_shell(
    tmp_path: Path,
) -> None:
    script = Path(__file__).with_name("install_owner_encoder_patch.sh")
    source = (
        Path(__file__).with_name("vendor") / "fixtures" / "telebot_node_reviewed_lf.js"
    ).read_bytes()
    root_stage = tmp_path / "root-stage"
    bin_dir, _ = _tools(tmp_path, source=source, root_stage=root_stage, fake_patcher=False)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OWNER_INSTALL_CALLS": str(tmp_path / "calls.jsonl"),
        "OWNER_ROOT_STAGE": str(root_stage),
    }

    rejected = subprocess.run(
        [
            str(bin_dir / "adb"),
            "-s",
            "robot",
            "push",
            "fixture",
            "/data/local/tmp/sweep-owner-patch.Ab12Cd34/telebot_node.js",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 1
    assert "Permission denied" in rejected.stderr
    (tmp_path / "calls.jsonl").unlink()

    subprocess.run(
        ["sh", str(script), "robot", NODE_DIR],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    from .tools.prepare_owner_encoder_patch import prepare

    assert stat.S_IMODE(root_stage.stat().st_mode) == 0o700
    assert root_stage.joinpath("telebot_node.js").read_bytes() == prepare(source)
    assert stat.S_IMODE(root_stage.joinpath("telebot_node.js").stat().st_mode) == 0o600
    assert (
        root_stage.joinpath("sweep_paired_encoder_sampler.js").read_bytes()
        == (Path(__file__).with_name("vendor") / "sweep_paired_encoder_sampler.js").read_bytes()
    )
    assert (
        stat.S_IMODE(root_stage.joinpath("sweep_paired_encoder_sampler.js").stat().st_mode) == 0o600
    )
    recorded = (tmp_path / "calls.jsonl").read_text().splitlines()
    calls = [tuple(json.loads(line)) for line in recorded]
    assert not any(args[2] == "push" for args, _ in calls)


def test_owner_install_reads_and_rehashes_through_one_root_shell_route(tmp_path: Path) -> None:
    calls = _run(str(Path(__file__).with_name("install_owner_encoder_patch.sh")), tmp_path)
    scripts = [script for _, script in calls]
    install = next(script for script in scripts if "cp -p $target $backup" in script)
    source_sha = hashlib.sha256(b"original").hexdigest()
    assert not any("exec-out" in args for args, _ in calls)
    source_call = next(
        index
        for index, (_, script) in enumerate(calls)
        if f"base64 {NODE_DIR}/telebot_node.js" in script
    )
    rehash_call = next(
        index
        for index, (_, script) in enumerate(calls)
        if f"sha256sum {NODE_DIR}/telebot_node.js" in script
    )
    transfer_call = next(
        index
        for index, (_, script) in enumerate(calls)
        if "base64 -d > /data/local/tmp/sweep-owner-patch.Ab12Cd34/telebot_node.js" in script
    )
    assert source_call < rehash_call < transfer_call
    assert not any(args[2] == "push" for args, _ in calls)
    assert f"= {source_sha} ]" in install
    assert "[ ! -e $disabled ]" in install
    assert (
        "[ \"$(stat -c '%u:%g:%a' /data/local/tmp/sweep-owner-patch.Ab12Cd34)\" = 0:0:700 ]"
        in install
    )
    assert (
        "[ \"$(stat -c '%a' /data/local/tmp/sweep-owner-patch.Ab12Cd34/telebot_node.js)\" = 600 ]"
        in install
    )
    assert "chown 1000:1000 $target $module" in install
    assert "chmod 600 $target $module" in install
    assert "chcon u:object_r:system_app_data_file:s0 $target $module" in install
    assert "stat -c '%u:%g:%a' $target" in install
    assert "ls -Zd $target" in install
    assert not any("run.sh start" in script for script in scripts)


def test_owner_install_refuses_transport_represented_bytes_that_do_not_rehash_remotely(
    tmp_path: Path,
) -> None:
    script = str(Path(__file__).with_name("install_owner_encoder_patch.sh"))
    mismatch = hashlib.sha256(b"different").hexdigest()
    with pytest.raises(subprocess.CalledProcessError):
        _run(script, tmp_path, remote_sha=mismatch)
    calls = [
        tuple(json.loads(line)) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert not any(args[2] == "push" for args, _ in calls)


def test_owner_rollback_pins_and_restores_both_reviewed_representations(tmp_path: Path) -> None:
    calls = _run(str(Path(__file__).with_name("rollback_owner_encoder_patch.sh")), tmp_path)
    rollback = next(
        script for _, script in calls if "telebot_node.js.sweep-owner-encoder.disabled" in script
    )
    assert (
        f"{CRLF_PATCHED_SHA}) patched_sha={CRLF_PATCHED_SHA}; reference_sha={CRLF_REFERENCE_SHA}"
        in rollback
    )
    assert (
        f"{LF_PATCHED_SHA}) patched_sha={LF_PATCHED_SHA}; reference_sha={LF_REFERENCE_SHA}"
        in rollback
    )
    assert "sha256sum $backup" in rollback
    assert "chown 1000:1000 $target" in rollback
    assert "chmod 600 $target" in rollback
    assert "chcon u:object_r:system_app_data_file:s0 $target" in rollback
    assert "rm $module $node_dir/telebot_node.js.sweep-owner-encoder.disabled" in rollback


def test_owner_rollback_pins_current_sampler_module() -> None:
    module = Path(__file__).with_name("vendor") / "sweep_paired_encoder_sampler.js"
    module_sha = hashlib.sha256(module.read_bytes()).hexdigest()
    rollback = Path(__file__).with_name("rollback_owner_encoder_patch.sh").read_text()
    assert f'module_sha="{module_sha}"' in rollback


def _rollback_script() -> str:
    content = Path(__file__).with_name("rollback_owner_encoder_patch.sh").read_text()
    return (
        content.split("cat <<EOF |", 1)[1]
        .split("\n", 1)[1]
        .rsplit("\nEOF", 1)[0]
        .replace("\\$", "$")
    )


@pytest.mark.parametrize(
    "source_sha,patched_sha",
    ((CRLF_REFERENCE_SHA, CRLF_PATCHED_SHA), (LF_REFERENCE_SHA, LF_PATCHED_SHA)),
)
def test_owner_rollback_restores_the_matching_reviewed_source_representation(
    tmp_path: Path, source_sha: str, patched_sha: str
) -> None:
    from .tools.prepare_owner_encoder_patch import prepare

    source = (
        Path(__file__).with_name("vendor") / "fixtures" / "telebot_node_reviewed_lf.js"
    ).read_bytes()
    if source_sha == CRLF_REFERENCE_SHA:
        source = source.replace(b"\n", b"\r\n")
    assert hashlib.sha256(source).hexdigest() == source_sha
    patched = prepare(source)
    assert hashlib.sha256(patched).hexdigest() == patched_sha
    remote = tmp_path / "node-files"
    remote.mkdir()
    target = remote / "telebot_node.js"
    backup = remote / "telebot_node.js.sweep-owner-encoder.backup"
    module = remote / "sweep_paired_encoder_sampler.js"
    target.write_bytes(patched)
    backup.write_bytes(source)
    module.write_bytes(
        (Path(__file__).with_name("vendor") / "sweep_paired_encoder_sampler.js").read_bytes()
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in {
        "chown": "exit 0\n",
        "chcon": "exit 0\n",
        "stat": "printf '1000:1000:600\\n'\n",
        "ls": "printf 'u:object_r:system_app_data_file:s0 %s\\n' \"$2\"\n",
    }.items():
        path = tools / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)
    remote_script = _rollback_script().replace("node_dir=$node_dir", f"node_dir={remote}")
    subprocess.run(
        ["sh"],
        input=remote_script,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "crlf_reference_sha": CRLF_REFERENCE_SHA,
            "lf_reference_sha": LF_REFERENCE_SHA,
            "crlf_patched_sha": CRLF_PATCHED_SHA,
            "lf_patched_sha": LF_PATCHED_SHA,
            "module_sha": hashlib.sha256(module.read_bytes()).hexdigest(),
            "vendor_owner": "1000:1000",
            "vendor_mode": "600",
            "vendor_context": "u:object_r:system_app_data_file:s0",
        },
        text=True,
        check=True,
    )
    assert target.read_bytes() == source
    assert not (remote / "telebot_node.js.sweep-owner-encoder.disabled").exists()
    assert not module.exists()
    assert not backup.exists()
