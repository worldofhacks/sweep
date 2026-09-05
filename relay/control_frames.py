from collections.abc import Mapping
from dataclasses import dataclass

from relay.auth import sign_event
from relay.control_localization import ControlLocalizationWire


@dataclass(frozen=True, slots=True)
class ControlLocalizationFrame:
    t: int
    event_id: str
    session: str
    wire: ControlLocalizationWire

    def to_event(self) -> dict[str, object]:
        return {
            **self.wire.to_mapping(),
            "t": self.t,
            "event_id": self.event_id,
            "session": self.session,
        }

    def unsigned_event(self) -> dict[str, object]:
        event = self.to_event()
        del event["signature"]
        return event

    @classmethod
    def parse(cls, raw: object) -> "ControlLocalizationFrame":
        if not isinstance(raw, Mapping):
            raise ValueError("localization frame must be an object")
        wire = ControlLocalizationWire.from_mapping(raw)
        if set(raw) != set(wire.to_mapping()) | {"t", "event_id", "session"}:
            raise ValueError("localization frame fields do not match the contract")
        if type(raw["t"]) is not int or raw["t"] < 0:
            raise ValueError("localization transport time must be nonnegative integer milliseconds")
        if any(
            not isinstance(raw[key], str) or not 1 <= len(raw[key]) <= 128
            for key in ("event_id", "session")
        ):
            raise ValueError("localization envelope identifiers are invalid")
        return cls(raw["t"], raw["event_id"], raw["session"], wire)


def sign_localization_frame(
    wire: ControlLocalizationWire,
    *,
    timestamp_ms: int,
    event_id: str,
    session: str,
    signing_key: bytes,
) -> dict[str, object]:
    frame = ControlLocalizationFrame(timestamp_ms, event_id, session, wire)
    unsigned = frame.unsigned_event()
    return {**unsigned, "signature": sign_event(unsigned, signing_key)}
