from collections.abc import Mapping
from dataclasses import dataclass

from relay.auth import sign_event, verify_event_signature
from relay.control_localization_contracts import (
    LOCALIZATION_BODY_FIELDS,
    ControlLocalizationWire,
    ControlPose,
    identifier,
    nonnegative_int64,
    session_identifier,
)

_LOCALIZATION_ENVELOPE_FIELDS = frozenset({"v", "t", "type", "event_id", "session", "signature"})


@dataclass(frozen=True, slots=True)
class ControlLocalizationFrame:
    t: int
    event_id: str
    session: str
    wire: ControlLocalizationWire
    signature: str

    def to_event(self) -> dict[str, object]:
        return {
            "v": 1,
            "t": self.t,
            "type": "control_localization",
            "event_id": self.event_id,
            "session": self.session,
            **self.wire.to_mapping(),
            "signature": self.signature,
        }

    def unsigned_event(self) -> dict[str, object]:
        event = self.to_event()
        event.pop("signature")
        return event

    def signature_valid(self, signing_key: bytes) -> bool:
        return verify_event_signature(self.unsigned_event(), self.signature, signing_key)

    @classmethod
    def parse(cls, raw: object) -> "ControlLocalizationFrame":
        expected = LOCALIZATION_BODY_FIELDS | _LOCALIZATION_ENVELOPE_FIELDS
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("localization frame must be an object")
        if type(raw["v"]) is not int or raw["v"] != 1:
            raise ValueError("localization v must be integer 1")
        if raw["type"] != "control_localization":
            raise ValueError("localization type is invalid")
        signature = raw["signature"]
        if not _signature(signature):
            raise ValueError("localization signature is invalid")
        body = {key: raw[key] for key in LOCALIZATION_BODY_FIELDS}
        return cls(
            nonnegative_int64(raw["t"], "t"),
            identifier(raw["event_id"], "event_id"),
            session_identifier(raw["session"]),
            ControlLocalizationWire.from_mapping(body),
            signature,
        )


def sign_localization_frame(
    wire: ControlLocalizationWire,
    *,
    timestamp_ms: int,
    event_id: str,
    session: str,
    signing_key: bytes,
) -> dict[str, object]:
    unsigned = ControlLocalizationFrame(
        nonnegative_int64(timestamp_ms, "timestamp_ms"),
        identifier(event_id, "event_id"),
        session_identifier(session),
        wire,
        "0" * 64,
    ).unsigned_event()
    return {**unsigned, "signature": sign_event(unsigned, signing_key)}


def sign_control_pose(pose: ControlPose, signing_key: bytes) -> dict[str, object]:
    """Sign the exact integer payload consumed by the repaired phone bridge."""
    unsigned = pose.unsigned_event()
    return {**unsigned, "signature": sign_event(unsigned, signing_key)}


def _signature(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )
