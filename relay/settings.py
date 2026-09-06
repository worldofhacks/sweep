"""Environment-backed relay configuration with no implicit hardware thresholds."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from relay.auth import StaticCredentialResolver
from relay.capabilities import C1_CAPABILITY_PROFILE, C2_CAPABILITY_PROFILE, CapabilityProfile
from relay.session import RelayLimits

DEFAULT_CONSOLE_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


class SettingsError(RuntimeError):
    pass


class AdapterBackend(StrEnum):
    SIM = "sim"
    REMOTE = "remote"


class CapabilityRelease(StrEnum):
    C1 = "c1"
    C2 = "c2"


@dataclass(frozen=True, slots=True)
class RelaySettings:
    relay_token: bytes = field(repr=False)
    adapter_keys: Mapping[int, bytes] = field(default_factory=dict, repr=False)
    allow_shared_adapter_token: bool = False
    localization_keys: Mapping[int, bytes] = field(default_factory=dict, repr=False)
    log_dir: Path = Path(".sweep/session-logs")
    intent_max_age_ms: int = 5_000
    transport_event_max_age_ms: int = 5_000
    future_clock_skew_ms: int = 1_000
    telemetry_freshness_ms: int = 1_000
    fanout_hz: int = 10
    adapter_backend: AdapterBackend = AdapterBackend.SIM
    capability_release: CapabilityRelease = CapabilityRelease.C1
    command_ttl_ms: int = 2_000
    command_deadline_ms: int = 10_000
    virtual_stick_hz: int = 10
    node_watchdog_hold_ms: int = 2_000
    node_watchdog_failsafe_ms: int = 10_000
    console_origins: tuple[str, ...] = DEFAULT_CONSOLE_ORIGINS
    # MediaMTX control API for the per-aircraft video projection; unset means node claims only.
    media_api_url: str | None = None
    media_api_username: str = "sweep-api"
    media_api_password: str | None = field(default=None, repr=False)
    media_api_timeout_ms: int = 500
    media_poll_interval_ms: int = 1_000
    media_stale_after_ms: int = 3_000
    # The console's media bootstrap served at GET /runtime-config.json; incomplete means 503.
    media_webrtc_origin: str | None = None
    media_read_username: str | None = None
    media_read_password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.relay_token) is not bytes or not 32 <= len(self.relay_token) <= 4_096:
            raise SettingsError("SWEEP_RELAY_TOKEN must contain 32 through 4096 bytes")
        if not isinstance(self.adapter_keys, Mapping) or not isinstance(
            self.localization_keys, Mapping
        ):
            raise SettingsError("aircraft credentials must be mappings")
        adapter_keys = dict(self.adapter_keys)
        localization_keys = dict(self.localization_keys)
        for label, keys in (
            ("adapter", adapter_keys),
            ("localization", localization_keys),
        ):
            if len(keys) > 64:
                raise SettingsError(f"{label} credentials exceed the 64-aircraft limit")
            if any(
                type(drone_id) is not int
                or not 1 <= drone_id <= 2**31 - 1
                or type(key) is not bytes
                or not 32 <= len(key) <= 4_096
                for drone_id, key in keys.items()
            ):
                raise SettingsError(
                    f"{label} IDs and credentials must satisfy the bounded aircraft contract"
                )
        credentials = [
            self.relay_token,
            *adapter_keys.values(),
            *localization_keys.values(),
        ]
        if len(set(credentials)) != len(credentials):
            raise SettingsError(
                "relay, adapter, and localization credentials must be globally distinct"
            )
        object.__setattr__(self, "adapter_keys", MappingProxyType(adapter_keys))
        object.__setattr__(self, "localization_keys", MappingProxyType(localization_keys))
        if self.fanout_hz != 10:
            raise SettingsError("state fan-out is frozen at 10 Hz")
        if not isinstance(self.adapter_backend, AdapterBackend):
            raise SettingsError("SWEEP_ADAPTER_BACKEND must be sim or remote")
        if not isinstance(self.capability_release, CapabilityRelease):
            raise SettingsError("SWEEP_CAPABILITY_RELEASE must be c1 or c2")
        if (
            self.capability_release is CapabilityRelease.C2
            and self.adapter_backend is not AdapterBackend.SIM
        ):
            raise SettingsError("SWEEP_CAPABILITY_RELEASE=c2 is allowed only with the sim backend")
        if not 5 <= self.virtual_stick_hz <= 25:
            raise SettingsError("SWEEP_VIRTUAL_STICK_HZ must be within the documented 5 to 25")
        if self.command_deadline_ms < self.command_ttl_ms:
            raise SettingsError("SWEEP_COMMAND_DEADLINE_MS must be at least SWEEP_COMMAND_TTL_MS")
        if (
            self.node_watchdog_hold_ms < 0
            or self.node_watchdog_failsafe_ms <= self.node_watchdog_hold_ms
        ):
            raise SettingsError("node watchdog thresholds must satisfy 0 <= hold < failsafe")
        RelayLimits(
            intent_max_age_ms=self.intent_max_age_ms,
            transport_event_max_age_ms=self.transport_event_max_age_ms,
            future_clock_skew_ms=self.future_clock_skew_ms,
            telemetry_freshness_ms=self.telemetry_freshness_ms,
            command_ttl_ms=self.command_ttl_ms,
        )
        _validate_origins(self.console_origins)
        if self.media_api_url is not None:
            if not _is_origin(self.media_api_url):
                raise SettingsError("SWEEP_MEDIA_API_URL must be an explicit HTTP(S) origin")
            if not self.media_api_username or not self.media_api_password:
                raise SettingsError(
                    "SWEEP_MEDIA_API_USERNAME and SWEEP_MEDIA_API_PASSWORD are required "
                    "when SWEEP_MEDIA_API_URL is set"
                )
        if self.media_api_timeout_ms <= 0 or self.media_poll_interval_ms <= 0:
            raise SettingsError(
                "SWEEP_MEDIA_API_TIMEOUT_MS and SWEEP_MEDIA_POLL_INTERVAL_MS must be positive"
            )
        if self.media_stale_after_ms < self.media_poll_interval_ms:
            raise SettingsError(
                "SWEEP_MEDIA_STALE_AFTER_MS must be at least SWEEP_MEDIA_POLL_INTERVAL_MS"
            )
        if self.media_webrtc_origin is not None and not _is_origin(self.media_webrtc_origin):
            raise SettingsError("SWEEP_MEDIA_WEBRTC_ORIGIN must be an explicit HTTP(S) origin")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RelaySettings:
        values = os.environ if environ is None else environ
        token = values.get("SWEEP_RELAY_TOKEN", "")
        if not token:
            raise SettingsError("SWEEP_RELAY_TOKEN is required")
        adapter_keys = _credential_keys(
            values.get("SWEEP_ADAPTER_KEYS_JSON", "{}"),
            "SWEEP_ADAPTER_KEYS_JSON",
        )
        return cls(
            relay_token=token.encode(),
            adapter_keys=adapter_keys,
            localization_keys=_credential_keys(
                values.get("SWEEP_LOCALIZATION_KEYS_JSON", "{}"),
                "SWEEP_LOCALIZATION_KEYS_JSON",
            ),
            allow_shared_adapter_token=_boolean(
                values.get("SWEEP_ALLOW_SHARED_ADAPTER_TOKEN", "false"),
                "SWEEP_ALLOW_SHARED_ADAPTER_TOKEN",
            ),
            log_dir=Path(values.get("SWEEP_SESSION_LOG_DIR", ".sweep/session-logs")),
            intent_max_age_ms=_positive_integer(
                values.get("SWEEP_INTENT_MAX_AGE_MS", "5000"), "SWEEP_INTENT_MAX_AGE_MS"
            ),
            transport_event_max_age_ms=_positive_integer(
                values.get("SWEEP_TRANSPORT_EVENT_MAX_AGE_MS", "5000"),
                "SWEEP_TRANSPORT_EVENT_MAX_AGE_MS",
            ),
            future_clock_skew_ms=_nonnegative_integer(
                values.get("SWEEP_FUTURE_CLOCK_SKEW_MS", "1000"),
                "SWEEP_FUTURE_CLOCK_SKEW_MS",
            ),
            telemetry_freshness_ms=_positive_integer(
                values.get("SWEEP_TELEMETRY_FRESHNESS_MS", "1000"),
                "SWEEP_TELEMETRY_FRESHNESS_MS",
            ),
            adapter_backend=_backend(values.get("SWEEP_ADAPTER_BACKEND", "sim")),
            capability_release=_capability_release(values.get("SWEEP_CAPABILITY_RELEASE", "c1")),
            command_ttl_ms=_positive_integer(
                values.get("SWEEP_COMMAND_TTL_MS", "2000"), "SWEEP_COMMAND_TTL_MS"
            ),
            command_deadline_ms=_positive_integer(
                values.get("SWEEP_COMMAND_DEADLINE_MS", "10000"), "SWEEP_COMMAND_DEADLINE_MS"
            ),
            virtual_stick_hz=_positive_integer(
                values.get("SWEEP_VIRTUAL_STICK_HZ", "10"), "SWEEP_VIRTUAL_STICK_HZ"
            ),
            node_watchdog_hold_ms=_nonnegative_integer(
                values.get("SWEEP_NODE_WATCHDOG_HOLD_MS", "2000"),
                "SWEEP_NODE_WATCHDOG_HOLD_MS",
            ),
            node_watchdog_failsafe_ms=_positive_integer(
                values.get("SWEEP_NODE_WATCHDOG_FAILSAFE_MS", "10000"),
                "SWEEP_NODE_WATCHDOG_FAILSAFE_MS",
            ),
            console_origins=_origins(
                values.get(
                    "SWEEP_CONSOLE_ORIGINS",
                    ",".join(DEFAULT_CONSOLE_ORIGINS),
                )
            ),
            media_api_url=_optional(values.get("SWEEP_MEDIA_API_URL")),
            media_api_username=_optional(values.get("SWEEP_MEDIA_API_USERNAME")) or "sweep-api",
            media_api_password=_optional(values.get("SWEEP_MEDIA_API_PASSWORD")),
            media_api_timeout_ms=_positive_integer(
                values.get("SWEEP_MEDIA_API_TIMEOUT_MS", "500"), "SWEEP_MEDIA_API_TIMEOUT_MS"
            ),
            media_poll_interval_ms=_positive_integer(
                values.get("SWEEP_MEDIA_POLL_INTERVAL_MS", "1000"),
                "SWEEP_MEDIA_POLL_INTERVAL_MS",
            ),
            media_stale_after_ms=_positive_integer(
                values.get("SWEEP_MEDIA_STALE_AFTER_MS", "3000"), "SWEEP_MEDIA_STALE_AFTER_MS"
            ),
            media_webrtc_origin=_optional(values.get("SWEEP_MEDIA_WEBRTC_ORIGIN")),
            media_read_username=_optional(values.get("SWEEP_MEDIA_READ_USERNAME")),
            media_read_password=_optional(values.get("SWEEP_MEDIA_READ_PASSWORD")),
        )

    def media_runtime_config(self) -> dict[str, str] | None:
        """The console's media bootstrap, or ``None`` until every value is configured."""
        if not (self.media_webrtc_origin and self.media_read_username and self.media_read_password):
            return None
        return {
            "webrtcOrigin": self.media_webrtc_origin,
            "readerUsername": self.media_read_username,
            "readerPassword": self.media_read_password,
        }

    def credential_resolver(self) -> StaticCredentialResolver:
        return StaticCredentialResolver(
            relay_token=self.relay_token,
            adapter_keys=self.adapter_keys,
            allow_shared_adapter_token=self.allow_shared_adapter_token,
            localization_keys=self.localization_keys,
        )

    def limits(self) -> RelayLimits:
        return RelayLimits(
            intent_max_age_ms=self.intent_max_age_ms,
            transport_event_max_age_ms=self.transport_event_max_age_ms,
            future_clock_skew_ms=self.future_clock_skew_ms,
            telemetry_freshness_ms=self.telemetry_freshness_ms,
            command_ttl_ms=self.command_ttl_ms,
        )

    def node_settings(self) -> dict[str, int]:
        """Thresholds the relay distributes to every node inside ``auth.accepted``."""
        return {
            "command_ttl_ms": self.command_ttl_ms,
            "virtual_stick_hz": self.virtual_stick_hz,
            "watchdog_hold_ms": self.node_watchdog_hold_ms,
            "watchdog_failsafe_ms": self.node_watchdog_failsafe_ms,
        }

    @property
    def capability_profile(self) -> CapabilityProfile:
        if self.capability_release is CapabilityRelease.C2:
            return C2_CAPABILITY_PROFILE
        return C1_CAPABILITY_PROFILE


def _credential_keys(raw: str, name: str) -> dict[int, bytes]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SettingsError(f"{name} must be valid JSON") from error
    if not isinstance(value, dict):
        raise SettingsError(f"{name} must be an object")
    result: dict[int, bytes] = {}
    for raw_id, raw_key in value.items():
        try:
            drone_id = int(raw_id)
        except (TypeError, ValueError):
            raise SettingsError(f"{name} IDs must be positive integers") from None
        if str(drone_id) != str(raw_id) or drone_id <= 0:
            raise SettingsError(f"{name} IDs must be canonical positive integers")
        if not isinstance(raw_key, str) or not raw_key:
            raise SettingsError(f"{name} credentials must be non-empty strings")
        result[drone_id] = raw_key.encode()
    return result


def _backend(raw: str) -> AdapterBackend:
    try:
        return AdapterBackend(raw)
    except ValueError:
        raise SettingsError("SWEEP_ADAPTER_BACKEND must be sim or remote") from None


def _capability_release(raw: str) -> CapabilityRelease:
    try:
        return CapabilityRelease(raw)
    except ValueError:
        raise SettingsError("SWEEP_CAPABILITY_RELEASE must be c1 or c2") from None


def _optional(raw: str | None) -> str | None:
    """An unset or blank variable is absent; the value is otherwise kept verbatim."""
    if raw is None or not raw.strip():
        return None
    return raw


def _boolean(raw: str, name: str) -> bool:
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    raise SettingsError(f"{name} must be true or false")


def _positive_integer(raw: str, name: str) -> int:
    value = _nonnegative_integer(raw, name)
    if value == 0:
        raise SettingsError(f"{name} must be positive")
    return value


def _nonnegative_integer(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer") from None
    if value < 0 or str(value) != raw:
        raise SettingsError(f"{name} must be a canonical non-negative integer")
    return value


def _origins(raw: str) -> tuple[str, ...]:
    origins = tuple(origin.strip() for origin in raw.split(",") if origin.strip())
    _validate_origins(origins)
    return origins


def _validate_origins(origins: tuple[str, ...]) -> None:
    if not origins or any(not _is_origin(origin) for origin in origins):
        raise SettingsError("SWEEP_CONSOLE_ORIGINS must contain explicit HTTP(S) origins")


def _is_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def console_origins_from_env(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    values = os.environ if environ is None else environ
    return _origins(values.get("SWEEP_CONSOLE_ORIGINS", ",".join(DEFAULT_CONSOLE_ORIGINS)))
