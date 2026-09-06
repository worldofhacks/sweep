import json
from dataclasses import asdict
from hashlib import sha256

from planner.navigation_runtime import NavigationRuntime


def navigation_metadata(runtime: NavigationRuntime) -> dict[str, object]:
    artifact = runtime.artifact()
    zones = [
        {
            "zone_id": zone.zone_id,
            "floor_id": zone.floor_id,
            "navigation_allowed": zone.navigation_allowed
            and zone.zone_id in runtime.permission.permitted_zone_ids,
            "arrival_slots": [slot.slot_id for slot in zone.arrival_slots],
            "aliases": list(zone.aliases),
        }
        for zone in artifact.zones
        if zone.floor_id == runtime.config.floor_id
    ]
    return {
        "map_pin": [artifact.map_pin.content_sha256, artifact.map_pin.version],
        "geometry_pin": [artifact.geometry_pin.content_sha256, artifact.geometry_pin.version],
        "configuration_id": sha256(
            json.dumps(asdict(runtime.config), sort_keys=True).encode()
        ).hexdigest(),
        "floor_id": runtime.config.floor_id,
        "catalog_version": sha256(json.dumps(zones, sort_keys=True).encode()).hexdigest(),
        "zones": zones,
    }
