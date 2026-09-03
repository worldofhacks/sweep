"""Environment-backed relay configuration with no implicit hardware thresholds."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from relay.auth import StaticCredentialResolver
from relay.session import RelayLimits

DEFAULT_CONSOLE_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RelaySettings:
    relay_token: bytes = field(repr=False)
    adapter_keys: Mapping[int, bytes] = field(default_factory=dict, repr=False)
    allow_shared_adapter_token: bool = False
    log_dir: Path = Path(".sweep/session-logs")
    intent_max_age_ms: int = 5_000
    transport_event_max_age_ms: int = 5_000
    future_clock_skew_ms: int = 1_000
    telemetry_freshness_ms: int = 1_000
    fanout_hz: int = 10
    console_origins: tuple[str, ...] = DEFAULT_CONSOLE_ORIGINS

    def __post_init__(self) -> None:
        if len(self.relay_token) < 32:
            raise SettingsError("SWEEP_RELAY_TOKEN must contain at least 32 characters")
        for drone_id, key in self.adapter_keys.items():
            if drone_id <= 0 or len(key) < 32:
                raise SettingsError("adapter IDs must be positive and keys at least 32 characters")
        if self.fanout_hz != 10:
            raise SettingsError("state fan-out is frozen at 10 Hz")
        RelayLimits(
            intent_max_age_ms=self.intent_max_age_ms,
            transport_event_max_age_ms=self.transport_event_max_age_ms,
            future_clock_skew_ms=self.future_clock_skew_ms,
            telemetry_freshness_ms=self.telemetry_freshness_ms,
        )
        _validate_origins(self.console_origins)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RelaySettings:
        values = os.environ if environ is None else environ
        token = values.get("SWEEP_RELAY_TOKEN", "")
        if not token:
            raise SettingsError("SWEEP_RELAY_TOKEN is required")
        adapter_keys = _adapter_keys(values.get("SWEEP_ADAPTER_KEYS_JSON", "{}"))
        return cls(
            relay_token=token.encode(),
            adapter_keys=adapter_keys,
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
            console_origins=_origins(
                values.get(
                    "SWEEP_CONSOLE_ORIGINS",
                    ",".join(DEFAULT_CONSOLE_ORIGINS),
                )
            ),
        )

    def credential_resolver(self) -> StaticCredentialResolver:
        return StaticCredentialResolver(
            relay_token=self.relay_token,
            adapter_keys=self.adapter_keys,
            allow_shared_adapter_token=self.allow_shared_adapter_token,
        )

    def limits(self) -> RelayLimits:
        return RelayLimits(
            intent_max_age_ms=self.intent_max_age_ms,
            transport_event_max_age_ms=self.transport_event_max_age_ms,
            future_clock_skew_ms=self.future_clock_skew_ms,
            telemetry_freshness_ms=self.telemetry_freshness_ms,
        )


def _adapter_keys(raw: str) -> dict[int, bytes]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SettingsError("SWEEP_ADAPTER_KEYS_JSON must be valid JSON") from error
    if not isinstance(value, dict):
        raise SettingsError("SWEEP_ADAPTER_KEYS_JSON must be an object")
    result: dict[int, bytes] = {}
    for raw_id, raw_key in value.items():
        try:
            drone_id = int(raw_id)
        except (TypeError, ValueError):
            raise SettingsError("adapter key IDs must be positive integers") from None
        if str(drone_id) != str(raw_id) or drone_id <= 0:
            raise SettingsError("adapter key IDs must be canonical positive integers")
        if not isinstance(raw_key, str) or not raw_key:
            raise SettingsError("adapter credentials must be non-empty strings")
        result[drone_id] = raw_key.encode()
    return result


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
