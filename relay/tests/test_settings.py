from __future__ import annotations

from pathlib import Path

import pytest

from relay.settings import AdapterBackend, RelaySettings, SettingsError
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY


def test_environment_builds_per_aircraft_credentials(tmp_path: Path) -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": f'{{"1":"{ADAPTER_KEY.decode()}"}}',
            "SWEEP_SESSION_LOG_DIR": str(tmp_path),
        }
    )

    assert settings.adapter_keys == {1: ADAPTER_KEY}
    assert settings.allow_shared_adapter_token is False
    assert settings.credential_resolver().resolve("adapter", 1) == ADAPTER_KEY


def test_missing_or_short_relay_token_fails_startup() -> None:
    with pytest.raises(SettingsError, match="required"):
        RelaySettings.from_env({})
    with pytest.raises(SettingsError, match="at least 32"):
        RelaySettings.from_env({"SWEEP_RELAY_TOKEN": "short"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWEEP_ALLOW_SHARED_ADAPTER_TOKEN", "sometimes"),
        ("SWEEP_TELEMETRY_FRESHNESS_MS", "0"),
        ("SWEEP_INTENT_MAX_AGE_MS", " 5000"),
    ],
)
def test_invalid_security_or_freshness_configuration_fails(name: str, value: str) -> None:
    environment = {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), name: value}

    with pytest.raises(SettingsError):
        RelaySettings.from_env(environment)


def test_bridge_settings_default_to_sim_and_relay_distributed_thresholds() -> None:
    settings = RelaySettings.from_env({"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode()})

    assert settings.adapter_backend is AdapterBackend.SIM
    assert settings.command_ttl_ms == 2_000
    assert settings.virtual_stick_hz == 10
    assert settings.node_watchdog_hold_ms == 2_000
    assert settings.node_watchdog_failsafe_ms == 10_000
    assert settings.limits().command_ttl_ms == 2_000
    assert settings.limits().require_issued_commands is False
    assert settings.node_settings() == {
        "command_ttl_ms": 2_000,
        "virtual_stick_hz": 10,
        "watchdog_hold_ms": 2_000,
        "watchdog_failsafe_ms": 10_000,
    }


def test_remote_backend_and_thresholds_come_from_the_environment() -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_BACKEND": "remote",
            "SWEEP_COMMAND_TTL_MS": "1500",
            "SWEEP_VIRTUAL_STICK_HZ": "20",
            "SWEEP_NODE_WATCHDOG_HOLD_MS": "500",
            "SWEEP_NODE_WATCHDOG_FAILSAFE_MS": "4000",
        }
    )

    assert settings.adapter_backend is AdapterBackend.REMOTE
    assert settings.limits().command_ttl_ms == 1_500
    assert settings.limits().require_issued_commands is True, "the relay issues every command"
    assert settings.node_settings() == {
        "command_ttl_ms": 1_500,
        "virtual_stick_hz": 20,
        "watchdog_hold_ms": 500,
        "watchdog_failsafe_ms": 4_000,
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SWEEP_ADAPTER_BACKEND", "hardware"),
        ("SWEEP_COMMAND_TTL_MS", "0"),
        ("SWEEP_VIRTUAL_STICK_HZ", "4"),
        ("SWEEP_VIRTUAL_STICK_HZ", "26"),
        ("SWEEP_NODE_WATCHDOG_FAILSAFE_MS", "2000"),
        ("SWEEP_NODE_WATCHDOG_HOLD_MS", "-1"),
    ],
)
def test_invalid_bridge_configuration_fails(name: str, value: str) -> None:
    environment = {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), name: value}

    with pytest.raises(SettingsError):
        RelaySettings.from_env(environment)


def test_command_deadline_defaults_above_the_ttl_and_must_cover_it() -> None:
    settings = RelaySettings.from_env({"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode()})

    assert settings.command_deadline_ms == 90_000, "must exceed node takeoff and landing"
    assert settings.limits().late_acknowledgement_window_ms == 90_000
    assert "command_deadline_ms" not in settings.node_settings()
    with pytest.raises(SettingsError, match="SWEEP_COMMAND_DEADLINE_MS"):
        RelaySettings.from_env(
            {
                "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
                "SWEEP_COMMAND_TTL_MS": "3000",
                "SWEEP_COMMAND_DEADLINE_MS": "2999",
            }
        )
