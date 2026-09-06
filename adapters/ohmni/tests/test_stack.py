"""Launcher ownership and port preconditions. Never signal or launch a real process."""

from __future__ import annotations

import json
import signal
import socket
from types import SimpleNamespace

import pytest

from adapters.ohmni.tools import stack


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "STATE", tmp_path)
    record = {
        "version": 1,
        "pid": 321,
        "pgid": 321,
        "marker": "relay.main",
        "started_at": "Sun Sep 6 17:00:00 2026",
    }
    (tmp_path / "relay.json").write_text(json.dumps(record))
    killed = []
    monkeypatch.setattr(stack.os, "killpg", lambda *args: killed.append(args))
    monkeypatch.setattr(stack.os, "getpgid", lambda _pid: 321)
    return record, killed


def identity(record, **changes):
    return {
        "pgid": record["pgid"],
        "started_at": record["started_at"],
        "command": "python -m relay.main --port 8010",
        **changes,
    }


def test_reused_pid_with_same_relay_marker_cannot_stop_another_session(owned, monkeypatch):
    record, killed = owned
    monkeypatch.setattr(
        stack,
        "process_identity",
        lambda _pid: identity(
            record, started_at="Sun Sep 6 17:30:00 2026", command="python -m relay.main --port 8000"
        ),
    )
    with pytest.raises(RuntimeError, match="another process"):
        stack.stop_host("relay")
    assert killed == []
    assert (stack.STATE / "relay.json").exists()


def test_old_incomplete_ownership_record_never_authorizes_signaling(owned):
    _, killed = owned
    (stack.STATE / "relay.json").write_text(json.dumps({"pid": 321, "marker": "relay.main"}))
    with pytest.raises(RuntimeError, match="complete process ownership"):
        stack.stop_host("relay")
    assert killed == []


def test_changed_group_is_rejected_even_with_matching_start_time(owned, monkeypatch):
    record, killed = owned
    monkeypatch.setattr(stack, "process_identity", lambda _pid: identity(record, pgid=654))
    with pytest.raises(RuntimeError, match="another process"):
        stack.stop_host("relay")
    assert killed == []


def test_confirmed_process_group_is_the_only_signaled_group(owned, monkeypatch):
    record, killed = owned
    observations = iter([identity(record), None])
    monkeypatch.setattr(stack, "process_identity", lambda _pid: next(observations))
    stack.stop_host("relay")
    assert killed == [(321, signal.SIGTERM)]
    assert not (stack.STATE / "relay.json").exists()


def test_process_identity_parses_stable_locale_start_time_and_group(monkeypatch):
    def run(args, **kwargs):
        assert args == ["ps", "-p", "321", "-o", "pgid=,lstart=,command="]
        assert kwargs["env"]["LC_ALL"] == "C"
        return SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=" 321 Sun Sep  6 17:00:00 2026 python -m relay.main --port 8010\n",
        )

    monkeypatch.setattr(stack.subprocess, "run", run)
    result = stack.process_identity(321)
    assert result["pgid"] == 321
    assert result["started_at"] == "Sun Sep 6 17:00:00 2026"


def test_start_host_records_start_time_and_new_process_group(owned, monkeypatch):
    record, killed = owned
    process = SimpleNamespace(pid=321)
    monkeypatch.setattr(stack.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(stack, "process_identity", lambda _pid: identity(record))
    assert stack.start_host("relay", ["unused"], stack.STATE, {}, "relay.main") is process
    assert stack.ownership_record("relay") == record
    assert killed == []


def test_occupied_port_is_rejected_without_connecting_to_its_service(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(
            stack, "listener_pids", lambda checked: {123} if checked == port else set()
        )
        with pytest.raises(RuntimeError, match=f"port {port} is occupied"):
            stack.require_ports_available((port,))


def test_listener_must_belong_to_the_recorded_process_group(owned, monkeypatch):
    record, killed = owned
    monkeypatch.setattr(
        stack,
        "process_identity",
        lambda pid: identity(record) if pid == 321 else identity(record, pgid=654),
    )
    monkeypatch.setattr(
        stack.subprocess,
        "run",
        lambda *_a, **_kw: SimpleNamespace(returncode=0, stdout="p999\n", stderr=""),
    )
    with pytest.raises(RuntimeError, match="unowned process"):
        stack.host_ready("relay", 8010)
    assert killed == []


def start_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "STATE", tmp_path / "state")
    monkeypatch.setattr(stack, "robots", lambda: [{"serial": "fake-only", "env_file": "unused"}])
    monkeypatch.setattr(
        stack,
        "env_file",
        lambda _: {
            "SWEEP_RELAY_ORIGIN": "ws://relay.example:8010",
            "SWEEP_RELAY_URL": "ws://relay.example:8010",
        },
    )
    monkeypatch.setenv("SWEEP_RELAY_ENV_FILE", str(tmp_path / "private.env"))
    monkeypatch.setattr("sys.argv", ["stack.py", "start"])
    operations = []
    monkeypatch.setattr(stack, "stop_host", lambda name: operations.append(("stop_host", name)))
    monkeypatch.setattr(stack.subprocess, "run", lambda *_a, **_kw: operations.append("compose"))
    monkeypatch.setattr(stack, "nodes", lambda *_a, **_kw: operations.append("nodes"))
    monkeypatch.setattr(stack, "start_host", lambda *_a, **_kw: operations.append("start_host"))
    return operations


def test_occupied_host_port_aborts_before_launch_or_robot_session_changes(tmp_path, monkeypatch):
    operations = start_fixture(tmp_path, monkeypatch)

    def occupied():
        raise RuntimeError("host port 8010 is occupied")

    monkeypatch.setattr(stack, "require_ports_available", occupied)
    with pytest.raises(RuntimeError, match="occupied"):
        stack.main()
    assert operations == [("stop_host", "console"), ("stop_host", "relay")]
    assert not (stack.STATE / "session").exists()


def test_bind_failure_after_preflight_does_not_restart_robots(tmp_path, monkeypatch):
    operations = start_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(stack, "require_ports_available", lambda: None)

    def launch(*_args, **_kwargs):
        operations.append("start_host")
        return SimpleNamespace(poll=lambda: 1)

    monkeypatch.setattr(stack, "start_host", launch)
    with pytest.raises(RuntimeError, match="exited before becoming ready"):
        stack.main()
    assert "nodes" not in operations
    assert not (stack.STATE / "session").exists()


@pytest.mark.parametrize(
    "value",
    [
        None,
        "ws://private.example:8000",
        "wss://private.example",
        "http://private.example:8010",
        "ws://user:key@private.example:8010",
    ],
)
def test_missing_or_wrong_console_origin_aborts_before_process_mutations(
    tmp_path, monkeypatch, value
):
    operations = start_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("SWEEP_RELAY_ORIGIN", raising=False)
    config = {"SWEEP_RELAY_URL": "ws://private.example:8010"}
    if value is not None:
        config["SWEEP_RELAY_ORIGIN"] = value
    monkeypatch.setattr(stack, "env_file", lambda _: config)
    with pytest.raises(ValueError, match="SWEEP_RELAY_ORIGIN.*port 8010") as error:
        stack.main()
    assert "private.example" not in str(error.value)
    assert operations == []


def test_wrong_robot_relay_port_aborts_before_any_host_or_robot_mutation(tmp_path, monkeypatch):
    operations = start_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        stack,
        "env_file",
        lambda _: {
            "SWEEP_RELAY_ORIGIN": "ws://private.example:8010",
            "SWEEP_RELAY_URL": "ws://private.example:8000",
        },
    )
    with pytest.raises(ValueError, match="SWEEP_RELAY_URL.*port 8010") as error:
        stack.main()
    assert "private.example" not in str(error.value)
    assert operations == []
