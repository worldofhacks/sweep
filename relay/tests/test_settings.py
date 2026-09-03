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
        }
    )

    assert settings.adapter_keys == {1: ADAPTER_KEY}
    assert settings.allow_shared_adapter_token is False
    assert settings.credential_resolver().resolve("adapter", 1) == ADAPTER_KEY


def test_media_admin_credential_enables_production_observation() -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_MEDIA_ADMIN_PASSWORD": "media-admin-secret",
            "SWEEP_MEDIA_STALE_AFTER_MS": "1500",
            "SWEEP_MEDIA_POLL_INTERVAL_MS": "250",
        }
    )

    assert settings.media_api_url == "http://127.0.0.1:9997"
    assert settings.media_admin_username == "sweep-admin"
    assert settings.media_admin_password == "media-admin-secret"
    assert settings.media_stale_after_ms == 1_500
    assert settings.media_poll_interval_ms == 250


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
