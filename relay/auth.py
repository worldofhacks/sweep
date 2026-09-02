"""Authentication and detached HMAC signatures for relay connections."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


class AuthenticationError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Principal:
    source: str
    drone_id: int | None
    signing_key: bytes = field(repr=False)


class CredentialResolver(Protocol):
    """Resolve a credential without coupling the relay to a hardware identity store."""

    def resolve(self, source: str, drone_id: int | None) -> bytes | None: ...


@dataclass(frozen=True, slots=True)
class StaticCredentialResolver:
    """Configuration-backed credentials used by the demo and tests.

    Adapter credentials are keyed by stable ``drone_id``.  Falling back to one
    relay credential is intentionally opt-in because it proves message integrity,
    not which aircraft authored a message.
    """

    relay_token: bytes = field(repr=False)
    adapter_keys: Mapping[int, bytes] = field(default_factory=dict, repr=False)
    allow_shared_adapter_token: bool = False

    def resolve(self, source: str, drone_id: int | None) -> bytes | None:
        if source in {"console", "keyboard"} and drone_id is None:
            return self.relay_token
        if source == "adapter" and drone_id is not None:
            key = self.adapter_keys.get(drone_id)
            if key is not None:
                return key
            if self.allow_shared_adapter_token:
                return self.relay_token
        return None


def authenticate(raw: object, resolver: CredentialResolver) -> Principal:
    """Authenticate a first WebSocket frame and bind its source/drone identity."""
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise AuthenticationError("invalid_auth", "authentication frame must be an object")
    source = raw.get("source")
    if source == "adapter":
        expected_fields = {"v", "type", "source", "drone_id", "token"}
    else:
        expected_fields = {"v", "type", "source", "token"}
    if set(raw) != expected_fields:
        raise AuthenticationError("invalid_auth", "authentication fields are invalid")
    if raw["v"] != 1 or isinstance(raw["v"], bool) or not isinstance(raw["v"], int):
        raise AuthenticationError("invalid_auth", "v must be integer 1")
    if raw["type"] != "auth":
        raise AuthenticationError("invalid_auth", "first frame must have type auth")
    if source not in {"console", "keyboard", "adapter"}:
        raise AuthenticationError("unknown_source", "source is not registered")
    token = raw["token"]
    if not isinstance(token, str) or not token:
        raise AuthenticationError("invalid_auth", "token must be a non-empty string")

    drone_id: int | None = None
    if source == "adapter":
        candidate = raw["drone_id"]
        if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate <= 0:
            raise AuthenticationError("invalid_auth", "drone_id must be a positive integer")
        drone_id = candidate

    expected = resolver.resolve(source, drone_id)
    supplied = token.encode()
    if expected is None or not hmac.compare_digest(supplied, expected):
        raise AuthenticationError("authentication_failed", "credential was not accepted")
    return Principal(source=source, drone_id=drone_id, signing_key=expected)


def sign_event(unsigned_event: Mapping[str, object], key: bytes | str) -> str:
    """Return HMAC-SHA256 over the documented canonical JSON representation."""
    secret = key.encode() if isinstance(key, str) else key
    return hmac.new(secret, canonical_event_bytes(unsigned_event), hashlib.sha256).hexdigest()


def verify_event_signature(
    unsigned_event: Mapping[str, object], signature: str, key: bytes
) -> bool:
    if (
        len(signature) != 64
        or signature != signature.lower()
        or any(character not in "0123456789abcdef" for character in signature)
    ):
        return False
    expected = sign_event(unsigned_event, key)
    return hmac.compare_digest(signature, expected)


def canonical_event_bytes(event: Mapping[str, object]) -> bytes:
    """Encode signed membership claims deterministically.

    Membership claims intentionally contain integers, booleans, strings, and
    string lists only, avoiding cross-language floating-point canonicalization.
    """
    try:
        serialized = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise AuthenticationError("invalid_signature_payload", str(error)) from None
    return serialized.encode("utf-8")
