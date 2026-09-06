from __future__ import annotations

from pathlib import Path

import pytest

from relay.capabilities import C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE
from relay.settings import AdapterBackend, CapabilityRelease, RelaySettings, SettingsError
from relay.tests.conftest import ADAPTER_KEY, CONSOLE_KEY


def test_environment_builds_per_aircraft_credentials(tmp_path: Path) -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_ADAPTER_KEYS_JSON": f'{{"1":"{ADAPTER_KEY.decode()}"}}',
            "SWEEP_LOCALIZATION_KEYS_JSON": f'{{"1":"{ADAPTER_KEY.decode()}-localization"}}',
            "SWEEP_SESSION_LOG_DIR": str(tmp_path),
            "SWEEP_CONSOLE_ORIGINS": "https://console.example,http://localhost:5173",
        }
    )

    assert settings.adapter_keys == {1: ADAPTER_KEY}
    assert settings.allow_shared_adapter_token is False
    assert settings.localization_keys == {1: ADAPTER_KEY + b"-localization"}
    assert settings.console_origins == ("https://console.example", "http://localhost:5173")
    assert settings.credential_resolver().resolve("adapter", 1) == ADAPTER_KEY
    assert settings.credential_resolver().resolve("localization", 1) == (
        ADAPTER_KEY + b"-localization"
    )


def test_aircraft_credential_configuration_is_immutable(tmp_path: Path) -> None:
    adapter_keys = {1: ADAPTER_KEY}
    localization_keys = {1: ADAPTER_KEY + b"-localization"}
    settings = RelaySettings(
        relay_token=CONSOLE_KEY,
        adapter_keys=adapter_keys,
        localization_keys=localization_keys,
        log_dir=tmp_path,
    )

    adapter_keys[2] = b"adapter-two-key-that-is-at-least-32"
    localization_keys.clear()

    assert settings.adapter_keys == {1: ADAPTER_KEY}
    assert settings.localization_keys == {1: ADAPTER_KEY + b"-localization"}
    with pytest.raises(TypeError):
        settings.localization_keys[2] = b"cannot-mutate-mapping"  # type: ignore[index]


def test_aircraft_credential_maps_are_bounded_and_reject_bool_ids(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="bounded aircraft contract"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            localization_keys={True: ADAPTER_KEY},
            log_dir=tmp_path,
        )
    with pytest.raises(SettingsError, match="64-aircraft"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            localization_keys={index: ADAPTER_KEY for index in range(1, 66)},
            log_dir=tmp_path,
        )


@pytest.mark.parametrize(
    ("adapter_keys", "localization_keys"),
    [
        ({1: CONSOLE_KEY}, {}),
        ({1: ADAPTER_KEY, 2: ADAPTER_KEY}, {}),
        ({1: ADAPTER_KEY}, {1: ADAPTER_KEY}),
        ({}, {1: CONSOLE_KEY}),
        ({}, {1: ADAPTER_KEY, 2: ADAPTER_KEY}),
    ],
)
def test_principal_credentials_must_be_globally_distinct(
    tmp_path: Path,
    adapter_keys: dict[int, bytes],
    localization_keys: dict[int, bytes],
) -> None:
    with pytest.raises(SettingsError, match="globally distinct"):
        RelaySettings(
            relay_token=CONSOLE_KEY,
            adapter_keys=adapter_keys,
            localization_keys=localization_keys,
            log_dir=tmp_path,
        )


def test_missing_or_short_relay_token_fails_startup() -> None:
    with pytest.raises(SettingsError, match="required"):
        RelaySettings.from_env({})
    with pytest.raises(SettingsError, match="32 through 4096"):
        RelaySettings.from_env({"SWEEP_RELAY_TOKEN": "short"})
    with pytest.raises(SettingsError, match="32 through 4096"):
        RelaySettings(relay_token=b"x" * 4_097)


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


def test_bridge_settings_default_to_sim_and_relay_distributed_thresholds() -> None:
    settings = RelaySettings.from_env({"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode()})

    assert settings.adapter_backend is AdapterBackend.SIM
    assert settings.capability_release is CapabilityRelease.C1
    assert settings.capability_profile is C1_CAPABILITY_PROFILE
    assert settings.command_ttl_ms == 2_000
    assert settings.virtual_stick_hz == 10
    assert settings.node_watchdog_hold_ms == 2_000
    assert settings.node_watchdog_failsafe_ms == 10_000
    assert settings.limits().command_ttl_ms == 2_000
    assert settings.node_settings() == {
        "command_ttl_ms": 2_000,
        "virtual_stick_hz": 10,
        "watchdog_hold_ms": 2_000,
        "watchdog_failsafe_ms": 10_000,
    }


def test_sim_c2_release_is_an_explicit_opt_in() -> None:
    settings = RelaySettings.from_env(
        {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), "SWEEP_CAPABILITY_RELEASE": "c2"}
    )

    assert settings.adapter_backend is AdapterBackend.SIM
    assert settings.capability_release is CapabilityRelease.C2
    assert settings.capability_profile is C2_CAPABILITY_PROFILE


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
    assert settings.node_settings() == {
        "command_ttl_ms": 1_500,
        "virtual_stick_hz": 20,
        "watchdog_hold_ms": 500,
        "watchdog_failsafe_ms": 4_000,
    }


def test_remote_release_binding_rejects_c2() -> None:
    with pytest.raises(SettingsError, match="allowed only with the sim backend"):
        RelaySettings.from_env(
            {
                "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
                "SWEEP_ADAPTER_BACKEND": "remote",
                "SWEEP_CAPABILITY_RELEASE": "c2",
            }
        )


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

    assert settings.command_deadline_ms == 10_000
    assert "command_deadline_ms" not in settings.node_settings()
    with pytest.raises(SettingsError, match="SWEEP_COMMAND_DEADLINE_MS"):
        RelaySettings.from_env(
            {
                "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
                "SWEEP_COMMAND_TTL_MS": "3000",
                "SWEEP_COMMAND_DEADLINE_MS": "2999",
            }
        )


def test_media_settings_default_to_node_claims_and_no_runtime_config() -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_MEDIA_API_URL": "",
            "SWEEP_MEDIA_API_PASSWORD": "   ",
            "SWEEP_MEDIA_WEBRTC_ORIGIN": "http://127.0.0.1:8889",
            "SWEEP_MEDIA_READ_USERNAME": "sweep-reader",
            "SWEEP_MEDIA_READ_PASSWORD": "",
        }
    )

    assert settings.media_api_url is None
    assert settings.media_api_username == "sweep-api"
    assert settings.media_api_password is None
    assert settings.media_api_timeout_ms == 500
    assert settings.media_poll_interval_ms == 1_000
    assert settings.media_stale_after_ms == 3_000
    assert settings.media_runtime_config() is None


def test_media_settings_come_from_the_environment_and_stay_out_of_repr() -> None:
    settings = RelaySettings.from_env(
        {
            "SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(),
            "SWEEP_MEDIA_API_URL": "http://127.0.0.1:9997",
            "SWEEP_MEDIA_API_USERNAME": "relay-api",
            "SWEEP_MEDIA_API_PASSWORD": "api-secret",
            "SWEEP_MEDIA_API_TIMEOUT_MS": "250",
            "SWEEP_MEDIA_POLL_INTERVAL_MS": "500",
            "SWEEP_MEDIA_STALE_AFTER_MS": "2000",
            "SWEEP_MEDIA_WEBRTC_ORIGIN": "http://10.10.1.60:8889",
            "SWEEP_MEDIA_READ_USERNAME": "sweep-reader",
            "SWEEP_MEDIA_READ_PASSWORD": "reader-secret",
        }
    )

    assert settings.media_api_url == "http://127.0.0.1:9997"
    assert settings.media_api_username == "relay-api"
    assert settings.media_api_password == "api-secret"
    assert settings.media_api_timeout_ms == 250
    assert settings.media_poll_interval_ms == 500
    assert settings.media_stale_after_ms == 2_000
    assert settings.media_runtime_config() == {
        "webrtcOrigin": "http://10.10.1.60:8889",
        "readerUsername": "sweep-reader",
        "readerPassword": "reader-secret",
    }
    assert "api-secret" not in repr(settings)
    assert "reader-secret" not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"SWEEP_MEDIA_API_URL": "http://127.0.0.1:9997"},
        {"SWEEP_MEDIA_API_URL": "http://127.0.0.1:9997/v3", "SWEEP_MEDIA_API_PASSWORD": "x"},
        {"SWEEP_MEDIA_API_URL": "http://user:pw@127.0.0.1:9997", "SWEEP_MEDIA_API_PASSWORD": "x"},
        {"SWEEP_MEDIA_API_TIMEOUT_MS": "0"},
        {"SWEEP_MEDIA_POLL_INTERVAL_MS": "2000", "SWEEP_MEDIA_STALE_AFTER_MS": "1000"},
        {"SWEEP_MEDIA_WEBRTC_ORIGIN": "http://ground-station:8889/drone1"},
    ],
)
def test_invalid_media_configuration_fails(overrides: dict[str, str]) -> None:
    environment = {"SWEEP_RELAY_TOKEN": CONSOLE_KEY.decode(), **overrides}

    with pytest.raises(SettingsError):
        RelaySettings.from_env(environment)
