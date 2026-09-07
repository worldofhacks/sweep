from __future__ import annotations

import json
import os
from dataclasses import asdict

import pytest

from relay.settings import SettingsError
from tests.autonomy_fixtures import planning_config, safety_config
from tools.fleet_relay import install_environment, load_configuration


def runtime_values():
    return {
        "SWEEP_RELAY_TOKEN": "console-private-test-credential-32bytes",
        "SWEEP_ADAPTER_KEYS_JSON": json.dumps({"11": "adapter-private-test-credential-32bytes"}),
        "SWEEP_NODE_TYPES_JSON": '{"11":"ground"}',
        "SWEEP_ADAPTER_BACKEND": "remote",
        "SWEEP_RELAY_ORIGIN": "ws://127.0.0.1:8010",
        "SWEEP_SESSION_ID": "real-runtime-config-test",
        "SWEEP_PLANNING_JSON": json.dumps(asdict(planning_config())),
        "SWEEP_SAFETY_JSON": json.dumps(asdict(safety_config())),
    }


def private_file(tmp_path, values):
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(values))
    path.chmod(0o600)
    return path


def test_real_runtime_validation_creates_no_nodes_or_services(tmp_path):
    _, settings, config = load_configuration(private_file(tmp_path, runtime_values()))
    assert set(settings.adapter_keys) == {11}
    assert config.supervised_vertical is None
    assert not (tmp_path / "session-logs").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"SWEEP_ADAPTER_BACKEND": "sim"},
        {"SWEEP_SIM_AIRCRAFT_COUNT": "4"},
        {"SWEEP_ALLOW_SHARED_ADAPTER_TOKEN": "true"},
        {"SWEEP_ADAPTER_KEYS_JSON": "{}"},
        {"SWEEP_SESSION_ID": ""},
        {"SWEEP_RELAY_ORIGIN": "ws://127.0.0.1:8011"},
        {"SWEEP_PLANNING_JSON": ""},
    ],
)
def test_operator_runtime_refuses_implicit_or_simulated_configuration(tmp_path, change):
    with pytest.raises((SettingsError, ValueError)):
        load_configuration(private_file(tmp_path, runtime_values() | change))


def test_runtime_credentials_must_be_private(tmp_path):
    path = private_file(tmp_path, runtime_values())
    path.chmod(0o644)
    with pytest.raises(SettingsError, match="private"):
        load_configuration(path)


def test_old_shell_sources_and_provider_keys_do_not_leak_into_new_deployment(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("SWEEP_WORLD_OBSERVATION_SOURCES", "old-world-source")
    monkeypatch.setenv("SWEEP_SESSION_ID", "old-session")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-key")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "old-telemetry-key")
    monkeypatch.setenv("FLEET_TEST_UNRELATED", "preserve")
    install_environment({"SWEEP_SESSION_ID": "new-session"})
    assert "SWEEP_WORLD_OBSERVATION_SOURCES" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "LANGFUSE_SECRET_KEY" not in os.environ
    assert os.environ["SWEEP_SESSION_ID"] == "new-session"
    assert os.environ["FLEET_TEST_UNRELATED"] == "preserve"
