"""Generate deterministic observation examples from the shared Python decoder."""

from __future__ import annotations

import json
from pathlib import Path

from spatial.contracts import FrameDeclaration
from spatial.observations import Observation, ObservationSubmission

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "observations-v1.json"


def vectors() -> dict[str, object]:
    common = {
        "v": 1,
        "type": "observation",
        "t": 1756700000050,
        "session": "mixed-observation-fixture",
        "connection_epoch": 2,
        "t_capture": 1756700000000,
        "confidence": 0.8,
    }
    cases = []
    for name, drone_id, node_type, source_id, frame, frame_kind, payload in (
        (
            "aircraft_world_pose",
            1,
            "aircraft",
            "aircraft-1-localization",
            "world",
            "world",
            {
                "kind": "pose",
                "position": {"frame": "world", "x_m": 1.0, "y_m": 2.0, "z_m": 1.5},
                "yaw_rad": None,
            },
        ),
        (
            "ground_odometry_pose",
            11,
            "ground_vehicle",
            "ohmni-11-odometry",
            "device:11:odom",
            "device_odometry",
            {
                "kind": "pose",
                "position": {"frame": "device:11:odom", "x_m": 0.3, "y_m": 0.0, "z_m": 0.0},
                "yaw_rad": 0.5,
            },
        ),
        (
            "ground_body_lidar",
            11,
            "ground_vehicle",
            "ohmni-11-lidar",
            "device:11:body",
            "device_body",
            {
                "kind": "lidar_scan",
                "angle_min_mdeg": -180000,
                "angle_increment_mdeg": 90000,
                "range_min_mm": 50,
                "range_max_mm": 12000,
                "ranges_mm": [1000, None, 450, 2300],
            },
        ),
    ):
        submission = common | {
            "event_id": name,
            "drone_id": drone_id,
            "node_type": node_type,
            "source_id": source_id,
            "frame": frame,
            "payload": payload,
        }
        accepted = Observation(
            ObservationSubmission.parse(submission),
            1756700000075,
            FrameDeclaration.parse({"id": frame, "kind": frame_kind}),
        ).to_dict()
        cases.append({"name": name, "submission": submission, "accepted": accepted})
    raw_scan = dict(cases[2]["submission"]) | {
        "event_id": "ground_raw_lidar",
        "frame": "device:11:lidar-raw",
    }
    cases.append(
        {
            "name": "ground_raw_lidar",
            "submission": raw_scan,
            "accepted": Observation(
                ObservationSubmission.parse(raw_scan),
                1756700000075,
                FrameDeclaration.parse({"id": "device:11:lidar-raw", "kind": "lidar_sensor"}),
            ).to_dict(),
        }
    )
    legacy_frame = "legacy:aircraft:1:epoch:2"
    legacy = common | {
        "event_id": "relay-legacy-aircraft-1",
        "drone_id": 1,
        "node_type": "aircraft",
        "source_id": "legacy.aircraft.1.telemetry",
        "frame": legacy_frame,
        "t_capture": None,
        "confidence": 0.0,
        "payload": {
            "kind": "legacy_aircraft_telemetry",
            "position": {"frame": legacy_frame, "x_m": 0.0, "y_m": 0.0, "z_m": 1.5},
            "vx_m_s": 0.0,
            "vy_m_s": 0.0,
            "vz_m_s": 0.0,
            "battery": 0.8,
            "state": "hovering",
            "link": 0.9,
            "pos_quality": 0.6,
            "capture_time_available": False,
        },
    }
    cases.append(
        {
            "name": "legacy_aircraft_send_time_only",
            "submission": None,
            "accepted": Observation(
                ObservationSubmission.parse(legacy, allow_legacy=True),
                1756700000075,
                FrameDeclaration.parse({"id": legacy_frame, "kind": "legacy_aircraft"}),
            ).to_dict(),
        }
    )
    return {"schema": "sweep.observation.v1", "cases": cases}


def main() -> None:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(vectors(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
