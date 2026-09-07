"""Run world-frame control localization over the publisher's authenticated socket."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType

from perception.control_localization import HeightObservation, TagFix, VelocityObservation
from perception.control_publisher import (
    ControlPublisher,
    ControlPublisherConfig,
    PublisherError,
    WebSocketPublisherTransport,
)
from perception.world_localization import (
    MeasurementUncertainty,
    WorldEnuTransform,
    WorldLocalizationAdapter,
    WorldLocalizationError,
    WorldLocalizationPins,
)
from relay.observations import ClockMapping, Observation, ObservationError

_MAX_CONFIG_BYTES = 1_048_576


def _unique_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("world localization configuration has a duplicate key")
        result[key] = value
    return result


def _mapping(value: object, fields: set[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{name} fields do not match the contract")
    return value


@dataclass(frozen=True, slots=True)
class WorldLocalizationRuntimeConfig:
    publisher: ControlPublisherConfig
    bundle: Path
    accepted_versions: Mapping[str, str]
    adapters: Mapping[int, WorldLocalizationAdapter]

    @classmethod
    def load(cls, path: str | Path) -> WorldLocalizationRuntimeConfig:
        source = Path(path)
        encoded = source.read_bytes()
        if len(encoded) > _MAX_CONFIG_BYTES:
            raise ValueError("world localization configuration exceeds 1 MiB")
        raw = json.loads(encoded, object_pairs_hook=_unique_json)
        expected = {"publisher", "bundle", "accepted_versions", "devices"}
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("world localization configuration fields do not match the contract")
        publisher = ControlPublisherConfig.from_mapping(raw["publisher"])
        if publisher.mode != "live":
            raise ValueError("world localization runtime requires the live publisher")
        if type(raw["bundle"]) is not str or not raw["bundle"]:
            raise ValueError("world localization bundle must be a path")
        if not isinstance(raw["accepted_versions"], Mapping):
            raise ValueError("accepted_versions must be a map")
        accepted_versions = dict(raw["accepted_versions"])
        if not isinstance(raw["devices"], list) or not raw["devices"]:
            raise ValueError("world localization devices must be a nonempty array")
        adapters: dict[int, WorldLocalizationAdapter] = {}
        for item in raw["devices"]:
            item = _mapping(item, {"pins", "capture_clock_mapping"}, "world localization device")
            pins = _pins(item["pins"])
            if pins.uncertainty.evidence_kind != "recorded_live":
                raise ValueError(
                    "runtime configuration requires recorded_live uncertainty evidence"
                )
            mapping = _clock_mapping(item["capture_clock_mapping"])
            adapter = WorldLocalizationAdapter(
                source.parent / raw["bundle"], accepted_versions, pins, mapping
            )
            publisher_drone = publisher.drones.get(pins.drone_id)
            if publisher_drone is None:
                raise ValueError("world localization device is absent from publisher configuration")
            fuser = publisher_drone.fuser
            if (
                fuser.map_id != pins.map_id
                or fuser.geometry_id != pins.geometry_id
                or fuser.clock_id != pins.capture_clock_mapping_id
                or fuser.tag_source_id != pins.tag_source_id
                or fuser.velocity_source_id != pins.telemetry_source_id
                or fuser.height_source_id != pins.telemetry_source_id
                or fuser.camera_calibration_id != pins.camera_calibration_id
                or fuser.body_extrinsics_id != pins.body_extrinsics_id
            ):
                raise ValueError("world localization pins do not match the publisher fuser")
            relay_clock = publisher_drone.clock_mapping
            if (
                relay_clock.capture_clock_id != pins.capture_clock_mapping_id
                or relay_clock.milliseconds_per_capture_second != 1_000
                or relay_clock.relay_reference_ms != round(relay_clock.capture_reference_s * 1_000)
            ):
                raise ValueError(
                    "publisher clock mapping must use relay milliseconds as capture seconds"
                )
            if pins.drone_id in adapters:
                raise ValueError("world localization device IDs must be unique")
            adapters[pins.drone_id] = adapter
        return cls(
            publisher=publisher,
            bundle=source.parent / raw["bundle"],
            accepted_versions=MappingProxyType(accepted_versions),
            adapters=MappingProxyType(adapters),
        )


def _clock_mapping(raw: object) -> ClockMapping:
    fields = {
        "mapping_id",
        "source_clock_id",
        "source_unit",
        "source_reference",
        "relay_reference_ms",
        "relay_ms_numerator",
        "source_units_denominator",
        "max_error_ms",
    }
    return ClockMapping(**dict(_mapping(raw, fields, "capture clock mapping")))


def _pins(raw: object) -> WorldLocalizationPins:
    fields = {
        "drone_id",
        "map_id",
        "map_version",
        "map_content_sha256",
        "geometry_id",
        "geometry_sha256",
        "physical_datum",
        "tag_source_id",
        "body_pose_source_id",
        "telemetry_source_id",
        "telemetry_frame_id",
        "height_datum_id",
        "height_alignment_measured",
        "capture_clock_mapping_id",
        "camera_calibration_id",
        "camera_calibration_sha256",
        "camera_pipeline_id",
        "body_extrinsics_id",
        "world_enu",
        "uncertainty",
    }
    value = _mapping(raw, fields, "world localization pins")
    transform = _mapping(
        value["world_enu"],
        {"transform_id", "matrix_world_enu", "measured"},
        "world ENU transform",
    )
    uncertainty = _mapping(
        value["uncertainty"],
        {
            "artifact_id",
            "sha256",
            "evidence_kind",
            "camera_calibration_id",
            "camera_pipeline_id",
            "position_covariance_world_m2",
            "velocity_covariance_enu_m2ps2",
            "height_variance_enu_m2",
        },
        "measurement uncertainty",
    )
    return WorldLocalizationPins(
        **{key: value[key] for key in fields - {"world_enu", "uncertainty"}},
        world_enu=WorldEnuTransform(**dict(transform)),
        uncertainty=MeasurementUncertainty(**dict(uncertainty)),
    )


class WorldLocalizationRuntime:
    """Translate admitted canonical observations and publish one fresh signed control frame."""

    def __init__(
        self, publisher: ControlPublisher, adapters: Mapping[int, WorldLocalizationAdapter]
    ):
        self.publisher = publisher
        self.adapters = MappingProxyType(dict(adapters))
        if self.publisher.config.mode != "live" or set(self.adapters) != set(
            self.publisher.config.drones
        ):
            raise ValueError("world runtime and live publisher aircraft must match")

    def tick(self, drone_id: int, monotonic_s: float) -> dict[str, object]:
        adapter = self.adapters.get(drone_id)
        if adapter is None:
            raise WorldLocalizationError("world localization drone is unconfigured")
        binding, events = self.publisher.take_live_observations(drone_id)
        for raw in events:
            try:
                measurements = adapter.ingest(
                    Observation.parse(raw), connection_epoch=binding.connection_epoch
                )
                for measurement in measurements:
                    self.publisher.enqueue(_sensor_record(measurement))
            except (
                ObservationError,
                WorldLocalizationError,
                PublisherError,
                TypeError,
                ValueError,
            ):
                self.publisher.refuse_input(
                    json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False),
                    "world_evidence_refused",
                )
        return self.publisher.publish_live(drone_id, monotonic_s)


def _sensor_record(measurement: object) -> dict[str, object]:
    if isinstance(measurement, TagFix):
        kind = "tag"
    elif isinstance(measurement, VelocityObservation):
        kind = "velocity"
    elif isinstance(measurement, HeightObservation):
        kind = "height"
    else:
        raise WorldLocalizationError("world adapter returned an unknown measurement")
    return {"kind": kind, **asdict(measurement)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--interval-s", type=float, default=0.1)
    args = parser.parse_args()
    if not 0 < args.interval_s <= 1:
        raise SystemExit("world localization interval must be within (0, 1] seconds")
    try:
        config = WorldLocalizationRuntimeConfig.load(args.config)
        assert config.publisher.websocket_url is not None
        publisher = ControlPublisher(
            config.publisher, WebSocketPublisherTransport(config.publisher.websocket_url)
        )
        publisher.bind_credentials()
        runtime = WorldLocalizationRuntime(publisher, config.adapters)
        while True:
            started = time.monotonic()
            for drone_id in config.adapters:
                runtime.tick(drone_id, started)
            remaining = args.interval_s - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, PublisherError, WorldLocalizationError) as error:
        raise SystemExit(f"world localization failed ({type(error).__name__})") from error
    finally:
        if "publisher" in locals():
            publisher.close()


if __name__ == "__main__":
    raise SystemExit(main())
