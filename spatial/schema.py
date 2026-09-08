"""Portable JSON Schema; stateful/source/frame checks remain receiver responsibilities."""

from __future__ import annotations

import json
from pathlib import Path

from spatial.contracts import MAX_TIMESTAMP, FrameKind, NodeType
from spatial.observations import MAX_RANGE_MM, MAX_SCAN_RANGES

SCHEMA_PATH = Path(__file__).with_name("fixtures") / "observation-v1.schema.json"


def object_schema(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def integer_schema(low=0, high=MAX_TIMESTAMP):
    return {"type": "integer", "minimum": low, "maximum": high}


def schema() -> dict[str, object]:
    text = {"type": "string", "minLength": 1, "maxLength": 128}
    fraction = {"type": "number", "minimum": 0, "maximum": 1}
    coordinate = {"type": "number", "minimum": -1000, "maximum": 1000}
    position = object_schema(
        {"frame": text, "x_m": coordinate, "y_m": coordinate, "z_m": coordinate}
    )
    payloads = [
        object_schema(
            {
                "kind": {"const": "pose"},
                "position": position,
                "yaw_rad": {
                    "type": ["number", "null"],
                    "minimum": -3.141592653589793,
                    "maximum": 3.141592653589793,
                },
            }
        ),
        object_schema(
            {
                "kind": {"const": "lidar_scan"},
                "angle_min_mdeg": integer_schema(-180000, 180000),
                "angle_increment_mdeg": integer_schema(1, 360000),
                "range_min_mm": integer_schema(1, MAX_RANGE_MM),
                "range_max_mm": integer_schema(1, MAX_RANGE_MM),
                "ranges_mm": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_SCAN_RANGES,
                    "items": {"anyOf": [integer_schema(1, MAX_RANGE_MM), {"type": "null"}]},
                },
            }
        ),
        object_schema(
            {
                "kind": {"const": "legacy_aircraft_telemetry"},
                "position": position,
                "vx_m_s": coordinate,
                "vy_m_s": coordinate,
                "vz_m_s": coordinate,
                "battery": fraction,
                "state": text,
                "link": fraction,
                "pos_quality": fraction,
                "capture_time_available": {"const": False},
            }
        ),
    ]
    nullable_identity = {"anyOf": [integer_schema(1, 2**31 - 1), {"type": "null"}]}
    result = object_schema(
        {
            "v": {"const": 1, "type": "integer"},
            "type": {"const": "observation"},
            "t": integer_schema(),
            "event_id": text,
            "session": text | {"maxLength": 512},
            "drone_id": integer_schema(1, 2**31 - 1),
            "connection_epoch": integer_schema(1, 2**31 - 1),
            "source_id": text,
            "node_type": {"enum": [item.value for item in NodeType]},
            "t_capture": {"anyOf": [integer_schema(), {"type": "null"}]},
            "t_ingest": integer_schema(),
            "frame": text,
            "confidence": fraction,
            "payload": {"oneOf": payloads},
            "authority": {"const": "diagnostic"},
            "frame_provenance": object_schema(
                {
                    "kind": {"enum": [item.value for item in FrameKind]},
                    "units": {"const": "m"},
                    "axes": {
                        "enum": [
                            "right_handed_z_up",
                            "x_forward_y_left_z_up",
                            "x_right_y_down_z_forward",
                            "unqualified_sensor_scan_angles",
                            "unqualified_legacy_planner_axes",
                        ]
                    },
                    "origin_drone_id": nullable_identity,
                    "origin_connection_epoch": nullable_identity,
                    "transform_id": {"type": "null"},
                }
            ),
        }
    )
    result.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:sweep:observation:v1",
            "title": "Sweep accepted diagnostic observation v1",
            "description": (
                "Also enforce 16 KiB UTF-8 limit, timestamp/capture ordering, declared exact "
                "frame/provenance, pose-frame equality, scan span and distance bounds, source "
                "binding, epochs and admission rates using the shared decoder and receiver."
            ),
            "allOf": [
                {
                    "if": {
                        "properties": {
                            "payload": {
                                "properties": {"kind": {"const": "legacy_aircraft_telemetry"}}
                            }
                        }
                    },
                    "then": {
                        "properties": {
                            "t_capture": {"type": "null"},
                            "confidence": {"const": 0},
                            "node_type": {"const": "aircraft"},
                            "frame_provenance": {
                                "properties": {"kind": {"const": "legacy_aircraft"}}
                            },
                        }
                    },
                    "else": {
                        "properties": {
                            "t_capture": integer_schema(),
                            "frame_provenance": {
                                "properties": {"kind": {"not": {"const": "legacy_aircraft"}}}
                            },
                        }
                    },
                }
            ],
        }
    )
    return result


def main() -> None:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(json.dumps(schema(), sort_keys=True, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
