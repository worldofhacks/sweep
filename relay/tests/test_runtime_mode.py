from __future__ import annotations

from pathlib import Path

import pytest

from adapters.dji_mini3.fake_node import FakeNodeConfig, parse_args
from adapters.dji_mini3.remote import RemoteBridgeAdapter
from adapters.sim.runtime import create_m14_sim_app
from relay.app import RelayRuntime
from relay.autonomy import AutonomyConfig
from relay.bridge import build_adapters
from relay.runtime_mode import TEST_ADAPTER_CAPABILITY
from relay.settings import AdapterBackend, RelaySettings, SettingsError
from relay.tests.conftest import (
    ADAPTER_KEY,
    CONSOLE_KEY,
    SESSION,
    EventIds,
    MutableClock,
    membership_payload,
)
from tests.autonomy_fixtures import make_snapshot


def test_hardware_defaults_have_no_generated_roster_or_simulated_dispatch(tmp_path: Path) -> None:
    settings = RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path)
    runtime = RelayRuntime(settings)
    assert settings.adapter_backend is AdapterBackend.REMOTE
    assert settings.allow_test_adapters is False
    assert runtime.session(SESSION).current_state()["drones"] == []
    pair = build_adapters(runtime, SESSION, make_snapshot())
    assert isinstance(pair.flight, RemoteBridgeAdapter)
    assert pair.camera is pair.flight


@pytest.mark.parametrize(
    "overrides",
    [
        {"SWEEP_ADAPTER_BACKEND": "sim"},
        {"SWEEP_ALLOW_SHARED_ADAPTER_TOKEN": "true"},
    ],
)
def test_environment_cannot_select_test_adapters_without_explicit_opt_in(overrides) -> None:
    with pytest.raises(SettingsError, match="SWEEP_ALLOW_TEST_ADAPTERS=true"):
        RelaySettings.from_env({"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), **overrides})
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ALLOW_TEST_ADAPTERS": "true",
            **overrides,
        }
    )
    assert settings.allow_test_adapters is True


@pytest.mark.parametrize("allow", ["yes", "1", "", " false "])
def test_test_mode_environment_flag_is_strict(allow: str) -> None:
    with pytest.raises(SettingsError, match="SWEEP_ALLOW_TEST_ADAPTERS"):
        RelaySettings.from_env(
            {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), "SWEEP_ALLOW_TEST_ADAPTERS": allow}
        )


def test_simulator_factory_refuses_before_constructing_any_fixture(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "adapters.sim.runtime._initial_snapshot",
        lambda _: pytest.fail("fixture generation reached"),
    )
    with pytest.raises(SettingsError, match="isolated test runtime"):
        create_m14_sim_app(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))


@pytest.mark.parametrize("allow", [False, True])
def test_signed_synthetic_join_is_host_gated_before_roster_admission(
    tmp_path: Path, adapter_principal, allow: bool
) -> None:
    runtime = RelayRuntime(
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys={1: ADAPTER_KEY},
            log_dir=tmp_path,
            allow_test_adapters=allow,
        ),
        clock=MutableClock(),
        event_ids=EventIds(),
    )
    session = runtime.session(SESSION)
    events = session.process_membership(
        membership_payload(
            action="join",
            event_id="synthetic-join",
            capabilities=["flight", TEST_ADAPTER_CAPABILITY],
        ),
        adapter_principal,
    )
    if allow:
        assert events[0]["type"] == "membership"
        assert session.current_state()["drones"][0]["adapter_capabilities"] == [
            "flight",
            TEST_ADAPTER_CAPABILITY,
        ]
    else:
        assert events[0]["reason"] == "test_adapter_disabled"
        assert session.current_state()["drones"] == []
        assert session.registry.roster_version == 0


def test_synthetic_rejoin_cannot_replace_an_existing_hardware_epoch(
    relay_session, adapter_principal
) -> None:
    relay_session.process_membership(
        membership_payload(action="join", event_id="hardware-join"), adapter_principal
    )
    before = relay_session.current_state()
    events = relay_session.process_membership(
        membership_payload(
            action="join",
            event_id="synthetic-rejoin",
            capabilities=["flight", TEST_ADAPTER_CAPABILITY],
        ),
        adapter_principal,
    )
    assert events[0]["reason"] == "test_adapter_disabled"
    after = relay_session.current_state()
    assert after["roster_version"] == before["roster_version"]
    assert after["drones"] == before["drones"]


def test_fake_config_always_declares_synthetic_provenance() -> None:
    config = FakeNodeConfig(
        relay_url="ws://127.0.0.1:18000",
        session="fixture",
        drone_id=1,
        token=ADAPTER_KEY.decode(),
        adapter_id="fixture",
        capabilities=("flight",),
    )
    assert config.capabilities == ("flight", TEST_ADAPTER_CAPABILITY)


CLI = ["--drone-id", "1", "--session", "fixture", "--relay", "ws://127.0.0.1:18000"]


@pytest.mark.parametrize("flag,environment", [(False, True), (True, False), (False, False)])
def test_fake_cli_requires_both_explicit_test_invocation_and_test_environment(
    monkeypatch, flag: bool, environment: bool
) -> None:
    monkeypatch.setenv("SWEEP_ALLOW_TEST_ADAPTERS", str(environment).lower())
    with pytest.raises(SystemExit) as failure:
        parse_args([*CLI, "--token", ADAPTER_KEY.decode(), *(["--test-only"] if flag else [])])
    assert failure.value.code == 2


def test_fake_cli_never_falls_back_to_the_operator_relay_token(monkeypatch) -> None:
    monkeypatch.setenv("SWEEP_ALLOW_TEST_ADAPTERS", "true")
    monkeypatch.setenv("SWEEP_RELAY_TOKEN", CONSOLE_KEY.decode())
    monkeypatch.setenv("SWEEP_ADAPTER_KEYS_JSON", "{}")
    with pytest.raises(SystemExit, match="no credential"):
        parse_args([*CLI, "--test-only"])
    config = parse_args([*CLI, "--test-only", "--token", ADAPTER_KEY.decode()])
    assert config.session == "fixture"
    assert config.token == ADAPTER_KEY.decode()


def test_operator_environment_example_has_no_simulator_or_measured_config_defaults() -> None:
    path = Path(__file__).parents[2] / ".env.example"
    environment = dict(
        line.partition("=")[::2]
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert environment["SWEEP_ADAPTER_BACKEND"] == "remote"
    assert environment["SWEEP_ALLOW_TEST_ADAPTERS"] == "false"
    assert environment["SWEEP_SESSION_ID"] == ""
    assert environment["SWEEP_RELAY_ORIGIN"] == ""
    assert environment["SWEEP_SIM_CAMERA_JSON"] == ""
    with pytest.raises(SettingsError, match="SWEEP_PLANNING_JSON is required"):
        AutonomyConfig.from_env(environment)
