from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from planner.navigation_contracts import integer, normalized_text, sha256_digest
from relay.auth import verify_event_signature


def content_digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class NavigationApproval:
    approval_id: str
    session: str
    mode: str
    configuration_sha256: str
    issued_at_ms: int
    expires_at_ms: int
    epochs: tuple[tuple[int, int], ...]
    evidence_sha256: tuple[str, ...]

    @classmethod
    def verify(cls, document: object, key: bytes) -> NavigationApproval:
        fields = {
            "v",
            "type",
            "approval_id",
            "session",
            "mode",
            "configuration_sha256",
            "issued_at_ms",
            "expires_at_ms",
            "epochs",
            "evidence_sha256",
            "signature",
        }
        if not isinstance(document, Mapping) or set(document) != fields:
            raise ValueError("navigation approval fields do not match the contract")
        unsigned = {name: value for name, value in document.items() if name != "signature"}
        if (
            not isinstance(key, bytes)
            or len(key) < 32
            or not verify_event_signature(unsigned, document["signature"], key)
        ):
            raise ValueError("navigation approval signature is invalid")
        if (
            type(document["v"]) is not int
            or document["v"] != 1
            or document["type"] != "navigation_approval"
        ):
            raise ValueError("navigation approval version or type is unsupported")
        for name in ("approval_id", "session"):
            normalized_text(document[name], name)
            if len(document[name]) > 128:
                raise ValueError(f"{name} exceeds the navigation bound")
        if document["mode"] not in {"simulation", "flight"}:
            raise ValueError("navigation approval mode is unsupported")
        sha256_digest(document["configuration_sha256"], "configuration_sha256")
        start = integer(document["issued_at_ms"], "issued_at_ms")
        end = integer(document["expires_at_ms"], "expires_at_ms")
        if not start < end <= start + 86_400_000:
            raise ValueError("navigation approval must expire within one day")
        epochs = document["epochs"]
        if not isinstance(epochs, list) or not 1 <= len(epochs) <= 4:
            raise ValueError("navigation approval needs one through four aircraft epochs")
        for pair in epochs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("navigation approval epoch must be a drone/epoch pair")
            integer(pair[0], "drone_id", minimum=1)
            integer(pair[1], "connection_epoch", minimum=1)
        if epochs != sorted(epochs) or len({pair[0] for pair in epochs}) != len(epochs):
            raise ValueError("navigation approval aircraft must be sorted and unique")
        evidence = document["evidence_sha256"]
        if (
            not isinstance(evidence, list)
            or len(evidence) > 16
            or evidence != sorted(set(evidence))
        ):
            raise ValueError("navigation approval evidence must be a bounded unique digest list")
        for digest in evidence:
            sha256_digest(digest, "evidence_sha256")
        if document["mode"] == "flight" and not evidence:
            raise ValueError("flight navigation approval requires external acceptance evidence")
        result = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            value = document[name]
            if name == "epochs":
                value = tuple(tuple(pair) for pair in value)
            elif name == "evidence_sha256":
                value = tuple(value)
            object.__setattr__(result, name, value)
        return result

    def check(
        self,
        *,
        session: str,
        configuration_sha256: str,
        now_ms: int,
        epochs: tuple[tuple[int, int], ...],
    ) -> None:
        if self.session != session or self.configuration_sha256 != configuration_sha256:
            raise ValueError(
                "navigation approval does not bind the active session and configuration"
            )
        if not self.issued_at_ms <= now_ms < self.expires_at_ms:
            raise ValueError("navigation approval is outside its validity period")
        if any(pair not in self.epochs for pair in epochs):
            raise ValueError("navigation aircraft epoch is outside the approval")
