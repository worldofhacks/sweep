from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from relay.observations import (
    ClockMapping,
    FrameDeclaration,
    FrameRegistry,
    Observation,
    ObservationError,
    ObservationSubmission,
    RatePolicy,
    SourceBinding,
    SourceTime,
    TimingPolicy,
    ingest,
)

MAX_SESSION_OBSERVATIONS = 1_000_000


@dataclass(frozen=True, slots=True)
class ObservationConfiguration:
    bindings: tuple[SourceBinding, ...]
    frames: FrameRegistry
    clock_mappings: tuple[ClockMapping, ...] = ()
    minimum_interval_ms: int = 10

    def __post_init__(self) -> None:
        bindings = tuple(self.bindings)
        keys = [(b.session, b.device_id, b.connection_epoch, b.source_id) for b in bindings]
        if not 1 <= len(keys) <= 128 or len(set(keys)) != len(keys):
            raise ValueError("observation bindings must be unique and bounded to 128")
        node_types: dict[int, str] = {}
        for binding in bindings:
            if node_types.setdefault(binding.device_id, binding.node_type) != binding.node_type:
                raise ValueError("one device must have one configured node type")
        mappings = tuple(self.clock_mappings)
        ids = [mapping.mapping_id for mapping in mappings]
        if len(ids) > 128 or len(set(ids)) != len(ids):
            raise ValueError("clock mappings must be unique and bounded to 128")
        if any(set(b.allowed_clock_mapping_ids) - set(ids) for b in bindings):
            raise ValueError("binding references an unconfigured clock mapping")
        RatePolicy(self.minimum_interval_ms)
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "clock_mappings", mappings)

    @classmethod
    def load(cls, path: Path) -> ObservationConfiguration:
        with path.open("rb") as stream:
            encoded = stream.read(1_048_577)
        if len(encoded) > 1_048_576:
            raise ValueError("observation configuration exceeds 1 MiB")

        def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate observation configuration key")
                result[key] = value
            return result

        raw = json.loads(encoded, object_pairs_hook=unique)
        if not isinstance(raw, dict) or set(raw) != {
            "bindings",
            "frames",
            "clock_mappings",
            "minimum_interval_ms",
        }:
            raise ValueError("invalid observation configuration fields")
        for name, limit in (("bindings", 128), ("frames", 256), ("clock_mappings", 128)):
            if not isinstance(raw[name], list) or len(raw[name]) > limit:
                raise ValueError(f"invalid observation configuration {name}")
        bindings = []
        for item in raw["bindings"]:
            if not isinstance(item, dict):
                raise ValueError("observation binding must be an object")
            item = dict(item)
            for name in ("allowed_frames", "allowed_payload_kinds", "allowed_clock_mapping_ids"):
                if name in item:
                    if not isinstance(item[name], list):
                        raise ValueError(f"{name} must be an array")
                    item[name] = tuple(item[name])
            bindings.append(SourceBinding(**item))
        return cls(
            bindings=tuple(bindings),
            frames=FrameRegistry(tuple(FrameDeclaration(**item) for item in raw["frames"])),
            clock_mappings=tuple(ClockMapping(**item) for item in raw["clock_mappings"]),
            minimum_interval_ms=raw["minimum_interval_ms"],
        )


class ObservationIngress:
    """Session-locked admission; source watermarks advance only on accepted events."""

    def __init__(self, configuration: ObservationConfiguration, session: str, skew_ms: int):
        self.bindings = MappingProxyType(
            {
                (binding.device_id, binding.connection_epoch, binding.source_id): binding
                for binding in configuration.bindings
                if binding.session == session
            }
        )
        self.frames = configuration.frames
        self.mappings = MappingProxyType({m.mapping_id: m for m in configuration.clock_mappings})
        self.rate = RatePolicy(configuration.minimum_interval_ms)
        self.timing = TimingPolicy(skew_ms)
        self._watermarks: dict[tuple[int, int, str], tuple[SourceTime, int]] = {}
        self._event_ids: set[tuple[int, int, str, str]] = set()

    def accept(
        self, submission: ObservationSubmission, *, now: int, producer_role: str = "adapter"
    ) -> Observation:
        key = (submission.device_id, submission.connection_epoch, submission.source_id)
        binding = self.bindings.get(key)
        if binding is None:
            raise ObservationError(
                "source_not_configured", "observation source has no host binding"
            )
        if binding.producer_role != producer_role:
            raise ObservationError(
                "source_role_mismatch", "observation source belongs to another authenticated role"
            )
        observation = ingest(
            submission,
            t_ingest=now,
            frames=self.frames,
            binding=binding,
            mappings=self.mappings,
            timing=self.timing,
        )
        event_key = (*key, submission.event_id)
        if event_key in self._event_ids:
            raise ObservationError("replayed_observation", "source event ID was already admitted")
        if len(self._event_ids) >= MAX_SESSION_OBSERVATIONS:
            raise ObservationError(
                "observation_capacity_reached", "start a new session for more observations"
            )
        previous = self._watermarks.get(key)
        receipt = submission.t_source_receipt
        if previous is not None:
            timestamp, last_ingest = previous
            if (receipt.clock_id, receipt.unit) != (timestamp.clock_id, timestamp.unit):
                raise ObservationError(
                    "source_clock_changed", "source clock changed within its epoch"
                )
            if receipt.value < timestamp.value:
                raise ObservationError("stale_observation", "source receipt time moved backwards")
            if not self.rate.accepts(last_ingest, now):
                raise ObservationError(
                    "observation_rate_limited", "source exceeds its configured rate"
                )
        self._event_ids.add(event_key)
        self._watermarks[key] = (receipt, now)
        return observation

    def host_times(self, observation: Observation) -> tuple[int, int]:
        """Return host-clock receipt and capture times for an admitted observation."""
        submission = observation.submission
        if submission.clock_mapping_id is None or submission.t_capture is None:
            raise ObservationError(
                "capture_time_unavailable",
                "world projection requires a mapped producer capture time",
            )
        mapping = self.mappings.get(submission.clock_mapping_id)
        if mapping is None:
            raise ObservationError(
                "unknown_clock_mapping", "observation clock mapping is unavailable"
            )
        return mapping.relay_ms(submission.t_source_receipt), mapping.relay_ms(submission.t_capture)
