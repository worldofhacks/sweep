"""Admission and byte pinning for preview-only navigation artifacts."""

from __future__ import annotations

import io
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import numpy as np

from planner.navigation_contracts import (
    MAX_GRID_CELLS,
    ArrivalSlot,
    ArtifactPin,
    Connector,
    GridLevel,
    NavigationEvidence,
    Zone,
    finite_number,
    free_grid_covers_pose,
    polygon_points,
    preview_evidence,
    sha256_digest,
    volume_contains,
)
from tools.map_common import parse_document
from tools.map_validate import ValidatedBundle, validate_bundle

_MAX_REPORT_BYTES = 2_000_000
_MAX_GRID_BYTES = 2_000_000
_REPORT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "flight_approved",
        "evidence_kind",
        "bundle_version",
        "bundle_content_sha256",
        "authoring_sha256",
        "floor_id",
        "floor_elevation_m",
        "units",
        "cell_m",
        "origin_xy",
        "shape_yx",
        "row_direction",
        "column_direction",
        "blocked_value",
        "candidate_value",
        "hazard_margin_m",
        "wall_inset_m",
        "source_point_count",
        "geometry_point_count",
        "preview_point_limit",
        "bands_above_floor_m",
        "route",
        "formations",
        "atrium_recommendation",
        "preview_point_count",
        "files",
    }
)


@contextmanager
def _directory_descriptor(directory: Path) -> Iterator[int]:
    root = directory.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise ValueError("geometry directory cannot be opened") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("geometry path must be a directory")
        yield descriptor
    finally:
        os.close(descriptor)


def _bounded_bytes(directory_descriptor: int, filename: str, limit: int, name: str) -> bytes:
    """Read one confined, no-follow descriptor snapshot and bound those exact bytes."""
    if Path(filename).name != filename or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError(f"{name} must be a regular direct child")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise ValueError(f"{name} must be a regular direct child") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{name} must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(limit + 1)
    finally:
        os.close(descriptor)
    if len(payload) > limit:
        raise ValueError(f"{name} exceeds its size limit")
    return payload


@dataclass(frozen=True, slots=True)
class NavigationArtifact:
    map_pin: ArtifactPin
    geometry_pin: ArtifactPin
    navigation_pin: ArtifactPin
    evidence: NavigationEvidence
    grid_clearance_m: float
    geofence_polygon_xy: tuple[tuple[float, float], ...]
    geofence_z_min_m: float
    geofence_z_max_m: float
    grids: tuple[GridLevel, ...]
    zones: tuple[Zone, ...]
    connectors: tuple[Connector, ...] = ()
    dispatch_eligible: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not all(
            isinstance(pin, ArtifactPin)
            for pin in (self.map_pin, self.geometry_pin, self.navigation_pin)
        ):
            raise ValueError("navigation artifact pins must use ArtifactPin")
        if not isinstance(self.evidence, NavigationEvidence):
            raise ValueError("navigation artifact evidence must use NavigationEvidence")
        if not all(isinstance(value, tuple) for value in (self.grids, self.zones, self.connectors)):
            raise ValueError("navigation artifact collections must be immutable tuples")
        if not self.grids or not all(isinstance(grid, GridLevel) for grid in self.grids):
            raise ValueError("navigation artifact needs an immutable tuple of typed grids")
        if not all(isinstance(zone, Zone) for zone in self.zones) or not all(
            isinstance(connector, Connector) for connector in self.connectors
        ):
            raise ValueError("navigation zones and connectors must use contract types")
        clearance = finite_number(self.grid_clearance_m, "grid_clearance_m", positive=True)
        object.__setattr__(self, "grid_clearance_m", clearance)
        boundary = polygon_points(self.geofence_polygon_xy, "geofence polygon")
        object.__setattr__(self, "geofence_polygon_xy", boundary)
        low = finite_number(self.geofence_z_min_m, "geofence z_min_m")
        high = finite_number(self.geofence_z_max_m, "geofence z_max_m")
        if low >= high:
            raise ValueError("geofence altitude bounds must increase")
        object.__setattr__(self, "geofence_z_min_m", low)
        object.__setattr__(self, "geofence_z_max_m", high)
        if len({(grid.floor_id, grid.z_m) for grid in self.grids}) != len(self.grids):
            raise ValueError("grid levels must be unique")
        if len({zone.zone_id for zone in self.zones}) != len(self.zones):
            raise ValueError("zone ids must be unique")
        if len({connector.connector_id for connector in self.connectors}) != len(self.connectors):
            raise ValueError("connector ids must be unique")
        slot_ids = [slot.slot_id for zone in self.zones for slot in zone.arrival_slots]
        if len(set(slot_ids)) != len(slot_ids):
            raise ValueError("arrival slot ids must be globally unique")
        for slot in (slot for zone in self.zones for slot in zone.arrival_slots):
            if not volume_contains(
                slot.pose,
                boundary,
                low,
                high,
                slot.radius_m,
                slot.half_height_m,
            ):
                raise ValueError("arrival slot volume must be contained by the geofence")
            if not any(
                free_grid_covers_pose(grid, slot.pose, clearance, slot.half_height_m)
                for grid in self.grids
            ):
                raise ValueError("arrival slot has no free validated altitude band")
        object.__setattr__(
            self,
            "navigation_pin",
            ArtifactPin(
                self.navigation_pin.version,
                _navigation_configuration_sha256(
                    self.map_pin,
                    self.geometry_pin,
                    self.grid_clearance_m,
                    self.geofence_polygon_xy,
                    self.geofence_z_min_m,
                    self.geofence_z_max_m,
                    self.grids,
                    self.zones,
                    self.connectors,
                ),
            ),
        )

    @classmethod
    def from_geometry_directory(
        cls,
        bundle: str | Path,
        geometry_directory: str | Path,
        accepted_map_versions: dict[str, str],
        arrival_slots: tuple[ArrivalSlot, ...] = (),
        connectors: tuple[Connector, ...] = (),
    ) -> NavigationArtifact:
        """Load an accepted map plus pinned offline geometry as a non-dispatchable preview."""
        if not isinstance(accepted_map_versions, dict) or any(
            not isinstance(version, str)
            or not version
            or version != version.strip()
            or not isinstance(digest, str)
            for version, digest in accepted_map_versions.items()
        ):
            raise ValueError("accepted map versions must be a version-to-digest mapping")
        for digest in accepted_map_versions.values():
            sha256_digest(digest, "accepted map digest")
        if not isinstance(arrival_slots, tuple) or not all(
            isinstance(slot, ArrivalSlot) for slot in arrival_slots
        ):
            raise ValueError("arrival slots must be an immutable tuple of ArrivalSlot values")
        if not isinstance(connectors, tuple) or not all(
            isinstance(connector, Connector) for connector in connectors
        ):
            raise ValueError("connectors must be an immutable tuple of Connector values")
        try:
            validated = validate_bundle(bundle, accepted_map_versions)
            with _directory_descriptor(Path(geometry_directory)) as directory_descriptor:
                report_payload = _bounded_bytes(
                    directory_descriptor,
                    "geometry.json",
                    _MAX_REPORT_BYTES,
                    "geometry report",
                )
                report = parse_document(report_payload, "geometry.json")
                bands = _validate_geometry_report(report, validated)
                height, width = report["shape_yx"]
                grids = tuple(
                    _load_grid(directory_descriptor, report, band, height, width) for band in bands
                )
            map_pin = ArtifactPin(validated["bundle_version"], validated["content_sha256"])
            geometry_pin = ArtifactPin(
                report["authoring_sha256"], sha256(report_payload).hexdigest()
            )
            zones_document = validated.document("zones.yaml")
            slot_groups: dict[str, list[ArrivalSlot]] = {}
            for slot in arrival_slots:
                slot_groups.setdefault(slot.zone_id, []).append(slot)
            known_zone_ids = {item["id"] for item in zones_document["zones"]}
            if set(slot_groups) - known_zone_ids:
                raise ValueError("arrival slots reference a zone outside the accepted map")
            zones = tuple(
                Zone(
                    item["id"],
                    item["floor_id"],
                    item["owner_approved"],
                    tuple(tuple(point) for point in item["polygon"]),
                    item["z_min"],
                    item["z_max"],
                    tuple(sorted(slot_groups.get(item["id"], ()), key=lambda slot: slot.slot_id)),
                )
                for item in sorted(zones_document["zones"], key=lambda item: item["id"])
            )
            _validate_connectors_against_graph(connectors, zones_document["room_graph"])
            geofence = zones_document["geofence"]
            navigation_pin = ArtifactPin(
                "preview",
                _navigation_configuration_sha256(
                    map_pin,
                    geometry_pin,
                    report["hazard_margin_m"],
                    tuple(tuple(point) for point in geofence["polygon"]),
                    geofence["z_min"],
                    geofence["z_max"],
                    grids,
                    zones,
                    connectors,
                ),
            )
            return cls(
                map_pin,
                geometry_pin,
                navigation_pin,
                preview_evidence(report["evidence_kind"]),
                report["hazard_margin_m"],
                tuple(tuple(point) for point in geofence["polygon"]),
                geofence["z_min"],
                geofence["z_max"],
                grids,
                zones,
                connectors,
            )
        except (
            KeyError,
            TypeError,
            IndexError,
            OverflowError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError(f"invalid navigation artifact: {exc}") from exc


def _load_grid(
    directory_descriptor: int,
    report: dict,
    band: float,
    height: int,
    width: int,
) -> GridLevel:
    floor_id = report["floor_id"]
    name = f"grid_{floor_id}_{band:.1f}.npy"
    payload = _bounded_bytes(
        directory_descriptor,
        name,
        _MAX_GRID_BYTES,
        f"geometry grid {name}",
    )
    if report["files"][name] != sha256(payload).hexdigest():
        raise ValueError(f"geometry grid hash mismatch: {name}")
    try:
        header = io.BytesIO(payload)
        if np.lib.format.read_magic(header) != (1, 0):
            raise ValueError("unsupported NPY format")
        shape, fortran_order, dtype = np.lib.format.read_array_header_1_0(header)
        if dtype != np.dtype(np.uint8) or fortran_order or shape != (height, width):
            raise ValueError("grid header disagrees with the report")
        rows = np.load(io.BytesIO(payload), allow_pickle=False)
    except (EOFError, ValueError) as exc:
        raise ValueError(f"invalid geometry grid: {name}") from exc
    if rows.dtype != np.uint8 or rows.ndim != 2 or not np.isin(rows, (0, 1)).all():
        raise ValueError(f"invalid binary uint8 geometry grid: {name}")
    return GridLevel(
        floor_id,
        report["floor_elevation_m"] + band,
        (report["origin_xy"][0], report["origin_xy"][1]),
        report["cell_m"],
        width,
        height,
        frozenset((int(x), int(y)) for y, x in zip(*np.where(rows == 1), strict=True)),
    )


def _validate_geometry_report(report: object, validated: ValidatedBundle) -> tuple[float, ...]:
    if not isinstance(report, dict) or set(report) != _REPORT_FIELDS:
        raise ValueError("geometry report does not match schema version 1")
    exact = {
        "schema_version": 1,
        "status": "offline_authoring",
        "flight_approved": False,
        "units": "meters",
        "row_direction": "+y",
        "column_direction": "+x",
        "blocked_value": 1,
        "candidate_value": 0,
    }
    if any(
        type(report.get(key)) is not type(expected) or report.get(key) != expected
        for key, expected in exact.items()
    ):
        raise ValueError("geometry report status, units, axes, or cell semantics are unsupported")
    if report["evidence_kind"] not in {"synthetic", "surveyed"}:
        raise ValueError("geometry report evidence_kind is unsupported")
    if (
        report["bundle_version"] != validated["bundle_version"]
        or report["bundle_content_sha256"] != validated["content_sha256"]
    ):
        raise ValueError("geometry artifact does not match the accepted map")
    sha256_digest(report["authoring_sha256"], "geometry authoring_sha256")
    if report["floor_id"] not in validated["floor_ids"]:
        raise ValueError("geometry report floor is outside the accepted map")
    for name in ("floor_elevation_m", "cell_m", "hazard_margin_m", "wall_inset_m"):
        value = finite_number(
            report[name],
            name,
            positive=name in {"cell_m", "hazard_margin_m"},
        )
        if name == "wall_inset_m" and value < 0:
            raise ValueError("wall_inset_m must be nonnegative")
    origin = report["origin_xy"]
    if not isinstance(origin, list) or len(origin) != 2:
        raise ValueError("geometry origin_xy must have two coordinates")
    finite_number(origin[0], "origin x")
    finite_number(origin[1], "origin y")
    shape = report["shape_yx"]
    if (
        not isinstance(shape, list)
        or len(shape) != 2
        or any(type(value) is not int or value < 1 for value in shape)
        or shape[0] * shape[1] > MAX_GRID_CELLS
    ):
        raise ValueError("geometry shape_yx is invalid or exceeds the cell limit")
    counts = (
        "source_point_count",
        "geometry_point_count",
        "preview_point_limit",
        "preview_point_count",
    )
    if any(type(report[name]) is not int or report[name] < 0 for name in counts):
        raise ValueError("geometry evidence counts must be nonnegative integers")
    if (
        report["preview_point_limit"] < 1
        or report["preview_point_count"] > report["preview_point_limit"]
    ):
        raise ValueError("geometry preview count exceeds its declared bound")
    if not isinstance(report["route"], dict) or not isinstance(report["formations"], list):
        raise ValueError("geometry route and formation evidence are malformed")
    _validate_route_evidence(report["route"])
    if report["atrium_recommendation"] not in {
        "candidate_pending_measurements",
        "use_kitchen_only_if_accepted",
    }:
        raise ValueError("geometry recommendation is unsupported")
    raw_bands = report["bands_above_floor_m"]
    if not isinstance(raw_bands, list) or not 1 <= len(raw_bands) <= 64:
        raise ValueError("geometry altitude bands must be a bounded nonempty list")
    bands = tuple(finite_number(value, "altitude band", positive=True) for value in raw_bands)
    if list(bands) != sorted(set(bands)) or any(round(value, 1) != value for value in bands):
        raise ValueError("geometry altitude bands must be unique increasing tenths of a meter")
    files = report["files"]
    if not isinstance(files, dict) or any(
        not isinstance(name, str) or not name or not isinstance(value, str)
        for name, value in files.items()
    ):
        raise ValueError("geometry report file pins are invalid")
    for digest in files.values():
        sha256_digest(digest, "geometry file pin")
    expected_files = {
        *(f"grid_{report['floor_id']}_{band:.1f}.npy" for band in bands),
        f"geofence_{report['floor_id']}.json",
        "preview.html",
    }
    if set(files) != expected_files:
        raise ValueError("geometry report file manifest is not exact")
    return bands


def _validate_route_evidence(route: dict) -> None:
    if set(route) != {
        "geometry_clear",
        "outside_grid",
        "intersecting_cells",
        "blocked_cells",
        "tube",
        "tag_proximity",
    }:
        raise ValueError("geometry route evidence does not match schema version 1")
    if type(route["geometry_clear"]) is not bool or type(route["outside_grid"]) is not bool:
        raise ValueError("geometry route decisions must be booleans")
    if any(
        type(route[name]) is not int or route[name] < 0
        for name in ("intersecting_cells", "blocked_cells")
    ):
        raise ValueError("geometry route cell counts must be nonnegative integers")
    tube = route["tube"]
    if not isinstance(tube, dict) or set(tube) != {
        "centerline",
        "half_width_m",
        "z_min",
        "z_max",
    }:
        raise ValueError("geometry route tube is malformed")
    centerline = tube["centerline"]
    if (
        not isinstance(centerline, list)
        or not 2 <= len(centerline) <= 1000
        or any(not isinstance(point, list) or len(point) != 2 for point in centerline)
    ):
        raise ValueError("geometry route centerline is malformed")
    for point in centerline:
        finite_number(point[0], "route x")
        finite_number(point[1], "route y")
    finite_number(tube["half_width_m"], "route half width", positive=True)
    route_low = finite_number(tube["z_min"], "route z_min")
    route_high = finite_number(tube["z_max"], "route z_max")
    if route_low >= route_high:
        raise ValueError("geometry route altitude bounds must increase")
    proximity = route["tag_proximity"]
    if not isinstance(proximity, dict) or set(proximity) != {
        "status",
        "visibility_verified",
        "sample_spacing_max_m",
        "radius_m",
        "samples_outside_radius",
        "samples",
    }:
        raise ValueError("tag proximity evidence does not match schema version 1")
    if (
        proximity["status"] != "candidate_proximity_only"
        or proximity["visibility_verified"] is not False
    ):
        raise ValueError("tag proximity cannot claim verified camera visibility")
    finite_number(proximity["sample_spacing_max_m"], "tag sample spacing", positive=True)
    finite_number(proximity["radius_m"], "tag proximity radius", positive=True)
    samples = proximity["samples"]
    if (
        type(proximity["samples_outside_radius"]) is not int
        or not isinstance(samples, list)
        or not 0 <= proximity["samples_outside_radius"] <= len(samples)
    ):
        raise ValueError("tag proximity sample counts are malformed")
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) != {"xyz", "nearest_tag_distance_m"}:
            raise ValueError("tag proximity sample is malformed")
        if not isinstance(sample["xyz"], list) or len(sample["xyz"]) != 3:
            raise ValueError("tag proximity sample XYZ is malformed")
        for value in sample["xyz"]:
            finite_number(value, "tag sample coordinate")
        distance = sample["nearest_tag_distance_m"]
        if distance is not None and finite_number(distance, "nearest tag distance") < 0:
            raise ValueError("nearest tag distance must be nonnegative")


def _validate_connectors_against_graph(connectors: tuple[Connector, ...], graph: dict) -> None:
    nodes = {node["id"]: node for node in graph["nodes"]}
    permitted_floor_pairs = {
        (nodes[edge["from"]]["floor_id"], nodes[edge["to"]]["floor_id"])
        for edge in graph["edges"]
        if edge["autonomous"] is True
    }
    permitted_floor_pairs |= {
        (destination, source) for source, destination in permitted_floor_pairs
    }
    if any(
        connector.enabled
        and (connector.from_floor_id, connector.to_floor_id) not in permitted_floor_pairs
        for connector in connectors
    ):
        raise ValueError("connector is outside the accepted autonomous room graph")


def _navigation_configuration_sha256(
    map_pin: ArtifactPin,
    geometry_pin: ArtifactPin,
    grid_clearance_m: float,
    geofence_polygon_xy: tuple[tuple[float, float], ...],
    geofence_z_min_m: float,
    geofence_z_max_m: float,
    grids: tuple[GridLevel, ...],
    zones: tuple[Zone, ...],
    connectors: tuple[Connector, ...],
) -> str:
    """Bind every public navigation-artifact value that can affect a route."""
    if not isinstance(map_pin, ArtifactPin) or not isinstance(geometry_pin, ArtifactPin):
        raise ValueError("configuration pins must use ArtifactPin")
    if not isinstance(grids, tuple) or not all(isinstance(grid, GridLevel) for grid in grids):
        raise ValueError("configuration grids must be an immutable tuple of GridLevel values")
    if not isinstance(zones, tuple) or not all(isinstance(zone, Zone) for zone in zones):
        raise ValueError("configuration zones must be an immutable tuple of Zone values")
    if not isinstance(connectors, tuple) or not all(
        isinstance(connector, Connector) for connector in connectors
    ):
        raise ValueError("connectors must be an immutable tuple of Connector values")
    payload = {
        "map_pin": [map_pin.version, map_pin.content_sha256],
        "geometry_pin": [geometry_pin.version, geometry_pin.content_sha256],
        "grid_clearance_m": grid_clearance_m,
        "geofence": {
            "polygon_xy": geofence_polygon_xy,
            "z_min_m": geofence_z_min_m,
            "z_max_m": geofence_z_max_m,
        },
        "grids": [
            {
                "floor_id": grid.floor_id,
                "z_m": grid.z_m,
                "origin_xy_m": grid.origin_xy_m,
                "cell_m": grid.cell_m,
                "width": grid.width,
                "height": grid.height,
                "blocked_cells": sorted(grid.blocked_cells),
            }
            for grid in sorted(grids, key=lambda item: (item.floor_id, item.z_m))
        ],
        "zones": [
            {
                "zone_id": zone.zone_id,
                "floor_id": zone.floor_id,
                "owner_approved": zone.owner_approved,
                "polygon_xy": zone.polygon_xy,
                "z_min_m": zone.z_min_m,
                "z_max_m": zone.z_max_m,
                "aliases": sorted(zone.aliases),
                "arrival_slots": [
                    {
                        "slot_id": slot.slot_id,
                        "zone_id": slot.zone_id,
                        "pose": [*slot.pose.xyz, slot.pose.floor_id],
                        "radius_m": slot.radius_m,
                        "half_height_m": slot.half_height_m,
                    }
                    for slot in sorted(zone.arrival_slots, key=lambda item: item.slot_id)
                ],
            }
            for zone in sorted(zones, key=lambda item: item.zone_id)
        ],
        "connectors": [
            {
                "connector_id": connector.connector_id,
                "from_floor_id": connector.from_floor_id,
                "to_floor_id": connector.to_floor_id,
                "from_pose": [*connector.from_pose.xyz, connector.from_pose.floor_id],
                "to_pose": [*connector.to_pose.xyz, connector.to_pose.floor_id],
                "enabled": connector.enabled,
            }
            for connector in sorted(connectors, key=lambda item: item.connector_id)
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
