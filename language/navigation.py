from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType

from relay.capabilities import CapabilityProfile

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"navigation {field} must be a safe identifier")
    return value


@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: str
    floor_id: str
    navigation_allowed: bool
    arrival_slots: tuple[str, ...]
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.zone_id, "zone id")
        _identifier(self.floor_id, "floor id")
        if not isinstance(self.navigation_allowed, bool):
            raise ValueError("navigation permission must be boolean")
        if not self.arrival_slots or len(set(self.arrival_slots)) != len(self.arrival_slots):
            raise ValueError("navigation zones require unique arrival slots")
        for slot in self.arrival_slots:
            _identifier(slot, "arrival slot")
        normalized = tuple(alias.strip().casefold() for alias in self.aliases)
        if any(not alias or len(alias) > 128 for alias in normalized) or len(
            set(normalized)
        ) != len(normalized):
            raise ValueError("navigation aliases must be unique bounded names")
        object.__setattr__(self, "aliases", normalized)

    def model_dict(self) -> dict[str, object]:
        return {
            "zone_id": self.zone_id,
            "floor_id": self.floor_id,
            "navigation_allowed": self.navigation_allowed,
            "arrival_slots": list(self.arrival_slots),
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True, slots=True)
class NavigationGrounding:
    capability_profile: CapabilityProfile
    map_pin: tuple[str, str]
    geometry_pin: tuple[str, str]
    configuration_id: str
    floor_id: str
    catalog_version: str
    zones: tuple[Zone, ...]
    formations: tuple[tuple[str, str], ...] = ()
    search_zones: tuple[str, ...] = ()
    target_classes: tuple[str, ...] = ()
    _aliases: MappingProxyType = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.capability_profile, CapabilityProfile):
            raise ValueError("navigation requires a capability profile")
        for pin in (self.map_pin, self.geometry_pin):
            if not isinstance(pin, tuple) or len(pin) != 2:
                raise ValueError("navigation pins require identity and version")
            _identifier(pin[0], "pin identity")
            _identifier(pin[1], "pin version")
        _identifier(self.configuration_id, "configuration id")
        _identifier(self.floor_id, "floor id")
        _identifier(self.catalog_version, "catalog version")
        if not self.zones or len({zone.zone_id for zone in self.zones}) != len(self.zones):
            raise ValueError("navigation catalog requires unique zones")
        zone_ids = {zone.zone_id for zone in self.zones}
        if any(
            _identifier(name, "formation name") != name or zone not in zone_ids
            for name, zone in self.formations
        ) or len({name for name, _ in self.formations}) != len(self.formations):
            raise ValueError("mapped formations require unique configured zones")
        if any(zone not in zone_ids for zone in self.search_zones) or len(
            set(self.search_zones)
        ) != len(self.search_zones):
            raise ValueError("search zones must be configured navigation zones")
        if any(
            target not in {"backpack", "bottle", "suitcase"} for target in self.target_classes
        ) or len(set(self.target_classes)) != len(self.target_classes):
            raise ValueError("search classes must be pretrained classes")
        aliases: dict[str, set[str]] = {}
        for zone in self.zones:
            for alias in (zone.zone_id.casefold(), *zone.aliases):
                aliases.setdefault(alias, set()).add(zone.zone_id)
        object.__setattr__(
            self,
            "_aliases",
            MappingProxyType({key: frozenset(value) for key, value in aliases.items()}),
        )

    def resolve(self, destination: str) -> tuple[Zone, ...]:
        if not isinstance(destination, str):
            return ()
        ids = self._aliases.get(destination.strip().casefold(), frozenset())
        return tuple(zone for zone in self.zones if zone.zone_id in ids)

    def model_dict(self) -> dict[str, object]:
        return {
            "map_pin": list(self.map_pin),
            "geometry_pin": list(self.geometry_pin),
            "configuration_id": self.configuration_id,
            "floor_id": self.floor_id,
            "catalog_version": self.catalog_version,
            "zones": [zone.model_dict() for zone in self.zones],
            "formations": [{"name": name, "zone_id": zone} for name, zone in self.formations],
            "search": {
                "zones": [{"zone_id": zone} for zone in self.search_zones],
                "target_classes": list(self.target_classes),
            },
        }

    def record_dict(self) -> dict[str, object]:
        return {
            **self.model_dict(),
            "capability_profile": self.capability_profile.name,
            "enabled_intent_names": sorted(
                name.value for name in self.capability_profile.enabled_intent_names
            ),
        }


def navigation_from_record(
    raw: object, capability_profile: CapabilityProfile
) -> NavigationGrounding:
    required = {
        "map_pin",
        "geometry_pin",
        "configuration_id",
        "floor_id",
        "catalog_version",
        "zones",
        "capability_profile",
        "enabled_intent_names",
    }
    if (
        not isinstance(raw, dict)
        or not required <= set(raw) <= required | {"formations", "search"}
        or raw["capability_profile"] != capability_profile.name
    ):
        raise ValueError("persisted navigation grounding is invalid")

    def pin(value: object) -> tuple[str, str]:
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("persisted navigation pin is invalid")
        return (_identifier(value[0], "pin identity"), _identifier(value[1], "pin version"))

    if not isinstance(raw["zones"], list):
        raise ValueError("persisted navigation catalog is invalid")
    zones = []
    for item in raw["zones"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"zone_id", "floor_id", "navigation_allowed", "arrival_slots", "aliases"}
            or not isinstance(item["arrival_slots"], list)
            or not isinstance(item["aliases"], list)
        ):
            raise ValueError("persisted navigation zone is invalid")
        zones.append(
            Zone(
                item["zone_id"],
                item["floor_id"],
                item["navigation_allowed"],
                tuple(item["arrival_slots"]),
                tuple(item["aliases"]),
            )
        )
    formations = raw.get("formations", [])
    search = raw.get("search", {"zones": [], "target_classes": []})
    if (
        not isinstance(formations, list)
        or not all(
            isinstance(item, dict) and set(item) == {"name", "zone_id"} for item in formations
        )
        or not isinstance(search, dict)
        or set(search) != {"zones", "target_classes"}
        or not isinstance(search["zones"], list)
        or not isinstance(search["target_classes"], list)
        or not all(isinstance(item, dict) and set(item) == {"zone_id"} for item in search["zones"])
    ):
        raise ValueError("persisted mission grounding is invalid")
    return NavigationGrounding(
        capability_profile,
        pin(raw["map_pin"]),
        pin(raw["geometry_pin"]),
        raw["configuration_id"],
        raw["floor_id"],
        raw["catalog_version"],
        tuple(zones),
        tuple((item["name"], item["zone_id"]) for item in formations),
        tuple(item["zone_id"] for item in search["zones"]),
        tuple(search["target_classes"]),
    )
