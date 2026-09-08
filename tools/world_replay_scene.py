"""Derive expiring Foxglove scene entities from explicitly framed audit evidence."""

import hashlib
import json
import math
from pathlib import Path

SCENE_SCHEMA = (
    Path(__file__).resolve().parents[1] / "schemas/foxglove/SceneUpdate.json"
).read_bytes()
_IDENTITY_POSE = {
    "position": {"x": 0.0, "y": 0.0, "z": 0.0},
    "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
}


def _pose(pose):
    return {
        "position": {axis: pose[f"{axis}_m"] for axis in "xyz"},
        "orientation": {axis: pose[f"q{axis}"] for axis in "xyzw"},
    }


def scene_update(record):
    event = record["event"]
    kind = event["type"]
    world_registration = event.get("registration") if kind == "world_observation" else None
    if world_registration is not None:
        event = event["observation"]
    ground_route = kind == "command" and event.get("operation") == "ground_navigate"
    if (
        kind not in {"observation", "world_observation", "navigation_route_authorization"}
        and not ground_route
    ):
        return None
    stamp = event.get("t_ingest", event.get("t"))
    timestamp = {"sec": stamp // 1000, "nsec": (stamp % 1000) * 1_000_000}
    identity = {
        name: event.get(name) for name in ("session", "device_id", "connection_epoch", "source_id")
    }
    identity["device_id"] = event.get("device_id", event.get("drone_id"))
    scope = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    entity = {
        "timestamp": timestamp,
        "frame_id": "",
        "id": scope,
        "frame_locked": True,
        "lifetime": {"sec": 1, "nsec": 0},
        "metadata": [
            {"key": key, "value": str(value)}
            for key, value in {
                **identity,
                "event_id": event["event_id"],
                "audit_seq": record["seq"],
                "display_lifetime": "1 second; diagnostic display only",
            }.items()
        ],
        **{
            key: []
            for key in (
                "arrows",
                "cubes",
                "spheres",
                "cylinders",
                "lines",
                "triangles",
                "texts",
                "models",
            )
        },
    }
    if world_registration is not None:
        entity["metadata"].extend(
            [
                {"key": "map_sha256", "value": world_registration["reference"]["contentHash"]},
                {"key": "registration_id", "value": world_registration["transformId"]},
                {"key": "floor_id", "value": world_registration["floorId"]},
            ]
        )
    if kind == "navigation_route_authorization" or ground_route:
        entity["id"] += "/route/" + event["command_id"]
        if ground_route:
            route = json.loads(event["args"]["navigation_route"])
            if (route["session"], route["device_id"], route["connection_epoch"]) != (
                event["session"],
                event["drone_id"],
                event["connection_epoch"],
            ):
                raise ValueError("ground route identity differs from audited command")
            frame = "world"
            points = [{"x": point["x_m"], "y": point["y_m"], "z": 0.0} for point in route["points"]]
            remaining = route["expires_at"] - stamp
            entity["metadata"].extend(
                [
                    {"key": "floor_id", "value": route["floor_id"]},
                    {"key": "projection", "value": "world XY projected onto display z=0"},
                    {"key": "map_version", "value": route["map_version"]},
                ]
            )
        else:
            frame = event["position_frame"]
            points = [
                {axis: segment[f"{end}_{axis}_mm"] / 1000 for axis in "xyz"}
                for segment in event["segments"]
                for end in ("start", "end")
            ]
            remaining = event["expires_at_ms"] - stamp
        entity["lines"] = [
            {
                "type": 0 if ground_route else 2,
                "pose": _IDENTITY_POSE,
                "thickness": 0.03,
                "scale_invariant": False,
                "points": points,
                "color": {"r": 0.4, "g": 0.7, "b": 1.0, "a": 1.0},
                "colors": [],
                "indices": [],
            }
        ]
        if remaining <= 0:
            return {
                "entities": [],
                "deletions": [{"timestamp": timestamp, "type": 0, "id": entity["id"]}],
            }
        entity["lifetime"] = {"sec": remaining // 1000, "nsec": remaining % 1000 * 1_000_000}
    else:
        payload = event["payload"]
        payload_kind = payload["kind"]
        entity["id"] += "/" + payload_kind
        if payload_kind == "tag_observation":
            entity["id"] += "/" + str(payload["tag_id"])
        if event["confidence"] <= 0:
            return {
                "entities": [],
                "deletions": [{"timestamp": timestamp, "type": 0, "id": entity["id"]}],
            }
        if payload_kind in {"pose", "telemetry", "tag_observation"}:
            if payload_kind == "pose" and world_registration is not None:
                frame = payload["position"]["frame"]
                pose = {
                    "position": {axis: payload["position"][f"{axis}_m"] for axis in "xyz"},
                    "orientation": {
                        "x": 0.0,
                        "y": 0.0,
                        "z": math.sin(payload["yaw_rad"] / 2),
                        "w": math.cos(payload["yaw_rad"] / 2),
                    },
                }
            elif payload_kind == "pose":
                pose, frame = _pose(payload["pose"]), payload["pose"]["parent_frame"]
            elif payload_kind == "telemetry":
                frame = payload["position"]["frame"]
                pose = {
                    **_IDENTITY_POSE,
                    "position": {axis: payload["position"][f"{axis}_m"] for axis in "xyz"},
                }
            else:
                if not payload["pose_accepted"]:
                    return None
                pose, frame = _pose(payload["tag_pose"]), payload["tag_pose"]["parent_frame"]
            entity["spheres"] = [
                {
                    "pose": pose,
                    "size": {axis: 0.16 for axis in "xyz"},
                    "color": {
                        "r": 0.2,
                        "g": 0.8,
                        "b": 0.5 if event["node_type"] == "ground" else 1.0,
                        "a": 1.0,
                    },
                }
            ]
        elif payload_kind == "range_scan":
            frame = payload["sensor_pose"]["parent_frame"]
            for index, distance in enumerate(payload["ranges_m"]):
                if distance is None:
                    continue
                angle = payload["angle_min_rad"] + index * payload["angle_increment_rad"]
                entity["spheres"].append(
                    {
                        "pose": {
                            **_IDENTITY_POSE,
                            "position": {
                                "x": distance * math.cos(angle),
                                "y": distance * math.sin(angle),
                                "z": 0.0,
                            },
                        },
                        "size": {axis: 0.035 for axis in "xyz"},
                        "color": {"r": 1.0, "g": 0.7, "b": 0.2, "a": 1.0},
                    }
                )
            pose = payload["sensor_pose"]
            qx, qy, qz, qw = (pose[f"q{axis}"] for axis in "xyzw")
            for sphere in entity["spheres"]:
                point = sphere["pose"]["position"]
                x, y = point["x"], point["y"]
                point.update(
                    {
                        "x": pose["x_m"]
                        + (1 - 2 * (qy * qy + qz * qz)) * x
                        + 2 * (qx * qy - qz * qw) * y,
                        "y": pose["y_m"]
                        + 2 * (qx * qy + qz * qw) * x
                        + (1 - 2 * (qx * qx + qz * qz)) * y,
                        "z": pose["z_m"]
                        + 2 * (qx * qz - qy * qw) * x
                        + 2 * (qy * qz + qx * qw) * y,
                    }
                )
        else:
            return None
    entity["frame_id"] = frame if frame == "world" else f"local/{scope}/{frame}"
    return {"entities": [entity], "deletions": []}
