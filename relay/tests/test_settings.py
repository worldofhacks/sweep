from __future__ import annotations

from pathlib import Path

import pytest

from relay.settings import RelaySettings, SettingsError
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY


def test_environment_builds_per_aircraft_credentials(tmp_path: Path) -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": f'{{"1":"{ADAPTER_KEY.decode()}"}}',
            "SWEEP_SESSION_LOG_DIR": str(tmp_path),
            "SWEEP_CONSOLE_ORIGINS": "https://console.example,http://localhost:5173",
        }
    )

    assert settings.adapter_keys == {1: ADAPTER_KEY}
    assert settings.allow_shared_adapter_token is False
    assert settings.console_origins == ("https://console.example", "http://localhost:5173")
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
        ("SWEEP_CONSOLE_ORIGINS", "*"),
    ],
)
def test_invalid_security_or_freshness_configuration_fails(name: str, value: str) -> None:
    environment = {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), name: value}

    with pytest.raises(SettingsError):
        RelaySettings.from_env(environment)
