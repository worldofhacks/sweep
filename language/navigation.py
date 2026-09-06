from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from relay.capabilities import CapabilityProfile

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_METADATA_FIELDS = {
    "map_pin",
    "geometry_pin",
    "configuration_id",
    "floor_id",
    "catalog_version",
    "zones",
}
_RECORD_FIELDS = _METADATA_FIELDS | {"capability_profile", "enabled_intent_names"}
_ZONE_FIELDS = {"zone_id", "floor_id", "navigation_allowed", "arrival_slots", "aliases"}


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"navigation {field} must be a safe identifier")
    return value


def _pin(value: object) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("navigation pins require identity and version")
    return (_identifier(value[0], "pin identity"), _identifier(value[1], "pin version"))


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
        if not isinstance(self.arrival_slots, tuple) or len(set(self.arrival_slots)) != len(
            self.arrival_slots
        ):
            raise ValueError("navigation zones require unique arrival slots")
        if self.navigation_allowed and not self.arrival_slots:
            raise ValueError("navigable zones require arrival slots")
        for slot in self.arrival_slots:
            _identifier(slot, "arrival slot")
        if not isinstance(self.aliases, tuple):
            raise ValueError("navigation aliases must be an immutable tuple")
        aliases = tuple(alias.strip().casefold() for alias in self.aliases)
        if any(not alias or len(alias) > 128 for alias in aliases) or len(set(aliases)) != len(
            aliases
        ):
            raise ValueError("navigation aliases must be unique bounded names")
        object.__setattr__(self, "aliases", aliases)

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
    _aliases: Mapping[str, frozenset[str]] = field(init=False, repr=False, compare=False)

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
        if (
            not isinstance(self.zones, tuple)
            or not self.zones
            or not all(isinstance(zone, Zone) for zone in self.zones)
            or len({zone.zone_id for zone in self.zones}) != len(self.zones)
        ):
            raise ValueError("navigation catalog requires unique zones")
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
        zone_ids = self._aliases.get(destination.strip().casefold(), frozenset())
        return tuple(zone for zone in self.zones if zone.zone_id in zone_ids)

    def model_dict(self) -> dict[str, object]:
        return {
            "map_pin": list(self.map_pin),
            "geometry_pin": list(self.geometry_pin),
            "configuration_id": self.configuration_id,
            "floor_id": self.floor_id,
            "catalog_version": self.catalog_version,
            "zones": [zone.model_dict() for zone in self.zones],
        }

    def record_dict(self) -> dict[str, object]:
        return {
            **self.model_dict(),
            "capability_profile": self.capability_profile.name,
            "enabled_intent_names": sorted(
                name.value for name in self.capability_profile.enabled_intent_names
            ),
        }


def navigation_from_metadata(
    raw: object, capability_profile: CapabilityProfile
) -> NavigationGrounding:
    if not isinstance(raw, Mapping) or set(raw) != _METADATA_FIELDS:
        raise ValueError("navigation metadata is invalid")
    if not isinstance(capability_profile, CapabilityProfile):
        raise ValueError("navigation requires a capability profile")
    zones = raw["zones"]
    if not isinstance(zones, list):
        raise ValueError("navigation catalog is invalid")
    parsed_zones = []
    for zone in zones:
        if (
            not isinstance(zone, Mapping)
            or set(zone) != _ZONE_FIELDS
            or not isinstance(zone["arrival_slots"], list)
            or not isinstance(zone["aliases"], list)
        ):
            raise ValueError("navigation zone is invalid")
        parsed_zones.append(
            Zone(
                zone["zone_id"],
                zone["floor_id"],
                zone["navigation_allowed"],
                tuple(zone["arrival_slots"]),
                tuple(zone["aliases"]),
            )
        )
    return NavigationGrounding(
        capability_profile,
        _pin(raw["map_pin"]),
        _pin(raw["geometry_pin"]),
        _identifier(raw["configuration_id"], "configuration id"),
        _identifier(raw["floor_id"], "floor id"),
        _identifier(raw["catalog_version"], "catalog version"),
        tuple(parsed_zones),
    )


def navigation_from_record(
    raw: object, capability_profile: CapabilityProfile
) -> NavigationGrounding:
    if (
        not isinstance(raw, Mapping)
        or set(raw) != _RECORD_FIELDS
        or raw["capability_profile"] != capability_profile.name
        or not isinstance(raw["enabled_intent_names"], list)
        or frozenset(raw["enabled_intent_names"])
        != frozenset(name.value for name in capability_profile.enabled_intent_names)
    ):
        raise ValueError("persisted navigation grounding is invalid")
    return navigation_from_metadata(
        {field: raw[field] for field in _METADATA_FIELDS}, capability_profile
    )
