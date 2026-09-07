"""Validate bounded, unapproved world-bundle candidates."""

from __future__ import annotations

import hashlib
import math
import os
import stat
import struct
from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from tools.geometry_math import point_inside, polygon, segment_distance
from tools.map_common import finite_number, parse_document, source_path, validate_transform
from tools.map_validate import content_hash
from tools.ohmni_world_registration import apply_transform, register_documents

_DOCUMENTS = ("manifest.yaml", "tags.yaml", "zones.yaml", "obstacles.yaml")
_MAX_DOCUMENT_BYTES = 1_000_000
_MAX_OCCUPANCY_BYTES = 10_000_000
_MAX_EVIDENCE_BYTES = 1_000_000
_MAX_EVIDENCE_FILES = 1024
_MAX_SOURCE_BYTES = 20_000_000
_MAX_TAGS = 512
_MAX_ZONES = 128
_MAX_OBSTACLES = 512
_MAX_CORRIDORS = 128
_MAX_POLYGON_VERTICES = 256
_MAX_CORRIDOR_POINTS = 256
_MAX_OBSERVATION_REFS = 64


class CandidateWorldBundle(Mapping[str, object]):
    """An immutable byte snapshot of a schema-v2 world bundle candidate."""

    def __init__(
        self,
        manifest: dict,
        documents: dict[str, bytes],
        sources: dict[str, bytes],
        registration: dict,
    ):
        self._manifest = deepcopy(manifest)
        self._documents = dict(documents)
        self._sources = dict(sources)
        self._registration = deepcopy(registration)

    def __getitem__(self, name: str) -> object:
        return deepcopy(self._manifest[name])

    def __iter__(self) -> Iterator[str]:
        return iter(self._manifest)

    def __len__(self) -> int:
        return len(self._manifest)

    def document(self, name: str) -> dict:
        if name not in self._documents:
            raise ValueError("unknown world-bundle document")
        return parse_document(self._documents[name], name)

    def occupancy_bytes(self) -> bytes:
        return self.source_bytes(self["occupancy"]["path"])

    def source_bytes(self, name: str) -> bytes:
        return self._sources[name]

    def registration(self) -> dict:
        return deepcopy(self._registration)

    def occupancy_cell_world_xy(self, column: int, row: int) -> tuple[float, float]:
        """Return the world XY center of a PNG/PGM pixel; invalid indices raise ValueError."""
        grid = self._manifest["occupancy"]
        _require(type(column) is int and 0 <= column < grid["width_cells"], "column outside grid")
        _require(type(row) is int and 0 <= row < grid["height_cells"], "row outside grid")
        x = grid["origin_xy"][0] + (column + 0.5) * grid["cell_m"]
        y = grid["origin_xy"][1] + (grid["height_cells"] - row - 0.5) * grid["cell_m"]
        return apply_transform(self._registration["T_target_source"], (x, y))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value: object, name: str, *, maximum: int = 128) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
        f"{name} must be nonempty text",
    )
    return value


def _sha256(value: object, name: str) -> str:
    _require(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{name} must be a lowercase SHA-256 digest",
    )
    return value


def _read_bounded(bundle: Path, name: str, limit: int, *, source: bool = False) -> bytes:
    if source:
        path = source_path(bundle, name)
    else:
        _require(name in _DOCUMENTS, "unknown world-bundle document")
        path = bundle / name
        _require(
            path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
            "world-bundle document must be a regular unlinked file",
        )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        _require(
            stat.S_ISREG(info.st_mode) and info.st_size <= limit, f"{name} exceeds its size limit"
        )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(limit + 1)
    finally:
        os.close(descriptor)
    _require(len(payload) <= limit, f"{name} exceeds its size limit")
    return payload


class _SourceSnapshots:
    def __init__(self, bundle: Path):
        self._bundle = bundle
        self.bytes: dict[str, bytes] = {}

    def read(self, path: object, digest: object, *, limit: int, name: str) -> bytes:
        path = _text(path, name, maximum=256)
        digest = _sha256(digest, f"{name} sha256")
        payload = self.bytes.get(path)
        if payload is None:
            _require(
                len(self.bytes) < _MAX_EVIDENCE_FILES, "world bundle exceeds source file limit"
            )
            payload = _read_bounded(self._bundle, path, limit, source=True)
            _require(
                sum(len(item) for item in self.bytes.values()) + len(payload) <= _MAX_SOURCE_BYTES,
                "world bundle exceeds source byte limit",
            )
            self.bytes[path] = payload
        _require(hashlib.sha256(payload).hexdigest() == digest, f"{name} hash mismatch")
        _require(len(payload) <= limit, f"{name} exceeds its size limit")
        return payload


def _bounded_number(value: object, name: str) -> float:
    number = finite_number(value, name)
    _require(abs(number) <= 1_000_000, f"{name} exceeds metric bounds")
    return number


def _point(value: object, dimensions: int, name: str) -> list[float]:
    _require(isinstance(value, list) and len(value) == dimensions, f"{name} has invalid dimensions")
    numbers = [_bounded_number(item, name) for item in value]
    return numbers


def _polygon(value: object, name: str) -> list[list[float]]:
    _require(
        isinstance(value, list) and 4 <= len(value) <= _MAX_POLYGON_VERTICES + 1,
        f"{name} needs a bounded closed polygon",
    )
    points = [_point(point, 2, name) for point in value]
    return polygon(points)


def _volume(value: object, name: str) -> tuple[list[list[float]], float, float]:
    _require(isinstance(value, dict), f"{name} must be an object")
    polygon = _polygon(value.get("polygon"), name)
    low = _bounded_number(value.get("z_min_m"), f"{name} z_min_m")
    high = _bounded_number(value.get("z_max_m"), f"{name} z_max_m")
    _require(low < high, f"{name} altitude bounds must increase")
    return polygon, low, high


def _header(document: dict, name: str) -> None:
    _require(
        document.get("schema_version") == 2 and type(document.get("schema_version")) is int,
        f"{name} must use schema_version 2",
    )
    _require(document.get("units") == "meters", f"{name} units must be meters")
    _require(document.get("frame") == "world", f"{name} frame must be world")


def _parse_pgm(payload: bytes, occupancy: dict) -> None:
    _require(occupancy.get("encoding") == "pgm-p5", "occupancy encoding must be pgm-p5")
    parts = payload.split(b"\n", 3)
    _require(len(parts) == 4 and parts[0] == b"P5", "occupancy must be a P5 PGM")
    try:
        width, height = (int(value) for value in parts[1].split())
        maximum = int(parts[2])
    except ValueError as exc:
        raise ValueError("occupancy PGM header is malformed") from exc
    _require(
        width == occupancy["width_cells"] and height == occupancy["height_cells"],
        "occupancy dimensions disagree with metadata",
    )
    _require(
        maximum == 255 and len(parts[3]) == width * height, "occupancy PGM payload is malformed"
    )
    _require(set(parts[3]) <= {0, 128, 255}, "occupancy contains an undefined pixel value")


def _parse_png(payload: bytes, occupancy: dict) -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    _require(len(payload) >= 33 and payload[:8] == signature, "occupancy must be a PNG")
    length = struct.unpack(">I", payload[8:12])[0]
    _require(length == 13 and payload[12:16] == b"IHDR", "occupancy PNG lacks an IHDR")
    width, height, depth, color, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", payload[16:29]
    )
    _require(
        width == occupancy["width_cells"] and height == occupancy["height_cells"],
        "occupancy dimensions disagree with metadata",
    )
    _require(
        (depth, color, compression, filter_method, interlace) == (8, 0, 0, 0, 0),
        "occupancy PNG must be noninterlaced Gray8",
    )
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    _require(
        isinstance(image, np.ndarray)
        and image.dtype == np.uint8
        and image.shape == (height, width),
        "occupancy PNG cannot be decoded as Gray8",
    )
    _require(
        not np.any((image != 0) & (image != 128) & (image != 255)),
        "occupancy contains an undefined pixel value",
    )


def _parse_occupancy(payload: bytes, occupancy: dict) -> None:
    encoding = occupancy.get("encoding")
    if encoding == "pgm-p5":
        _parse_pgm(payload, occupancy)
    elif encoding == "png-gray8":
        _parse_png(payload, occupancy)
    else:
        raise ValueError("occupancy encoding must be png-gray8 or pgm-p5")


def _evidence_document(payload: bytes, name: str) -> dict:
    try:
        return parse_document(payload, name)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a valid JSON evidence document") from exc


def _validate_manifest(
    manifest: dict, documents: dict[str, bytes], sources: _SourceSnapshots
) -> dict:
    _require(
        set(manifest)
        == {
            "schema_version",
            "bundle_kind",
            "bundle_version",
            "map_id",
            "created_at",
            "units",
            "frame",
            "occupancy",
            "registration",
            "files",
            "content_sha256",
        },
        "manifest does not match schema",
    )
    _require(
        type(manifest.get("schema_version")) is int and manifest["schema_version"] == 2,
        "world bundle must use schema_version 2",
    )
    _require(
        manifest.get("bundle_kind") == "world-bundle", "manifest bundle_kind must be world-bundle"
    )
    _require(manifest.get("units") == "meters", "manifest units must be meters")
    _text(manifest.get("bundle_version"), "bundle_version")
    _text(manifest.get("map_id"), "map_id")
    timestamp = _text(manifest.get("created_at"), "created_at")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    _require(
        parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0,
        "created_at must include UTC timezone",
    )
    frame = manifest.get("frame")
    _require(isinstance(frame, dict), "manifest frame must be an object")
    _require(
        frame
        == {
            "name": "world",
            "axis_convention": "right_handed_z_up",
            "x": "toward_elevator",
            "y": "toward_street_wall",
            "z": "up",
            "units": "meters",
            "physical_datum": "tag_0_center",
        },
        "manifest frame must be the canonical local world frame",
    )
    _require(
        set(manifest.get("files", {})) == set(_DOCUMENTS[1:]),
        "manifest files must list tags, zones, and obstacles",
    )
    for name in _DOCUMENTS[1:]:
        _require(
            _sha256(manifest["files"][name], f"{name} hash")
            == hashlib.sha256(documents[name]).hexdigest(),
            f"{name} hash mismatch",
        )
    _require(manifest.get("content_sha256") == content_hash(manifest), "content hash mismatch")
    occupancy = manifest.get("occupancy")
    _require(
        isinstance(occupancy, dict)
        and set(occupancy)
        == {
            "path",
            "sha256",
            "encoding",
            "width_cells",
            "height_cells",
            "cell_m",
            "origin_xy",
            "frame",
            "source_scope",
            "row_0",
            "pixels",
        },
        "occupancy metadata does not match schema",
    )
    width, height = occupancy["width_cells"], occupancy["height_cells"]
    _require(
        type(width) is int
        and type(height) is int
        and 1 <= width <= 4096
        and 1 <= height <= 4096
        and width * height <= _MAX_OCCUPANCY_BYTES,
        "occupancy dimensions exceed bounds",
    )
    _bounded_number(occupancy["cell_m"], "occupancy cell_m")
    _require(occupancy["cell_m"] > 0, "occupancy cell_m must be positive")
    _point(occupancy["origin_xy"], 2, "occupancy origin_xy")
    _point(
        [
            occupancy["origin_xy"][0] + width * occupancy["cell_m"],
            occupancy["origin_xy"][1] + height * occupancy["cell_m"],
        ],
        2,
        "occupancy extent",
    )
    _require(occupancy["row_0"] == "maximum_y", "occupancy row 0 must represent maximum local y")
    _require(
        occupancy["pixels"] == {"occupied": 0, "unknown": 128, "free": 255},
        "occupancy pixel legend is unsupported",
    )
    payload = sources.read(
        occupancy["path"],
        occupancy["sha256"],
        limit=_MAX_OCCUPANCY_BYTES,
        name="occupancy",
    )
    _parse_occupancy(payload, occupancy)
    registration = manifest.get("registration")
    _require(
        isinstance(registration, dict)
        and set(registration)
        == {"source", "observed_tags", "known_tags", "held_out_tag_ids", "maximum_residual_m"},
        "registration metadata does not match schema",
    )
    _require(registration["source"] == "ohmni_slam", "registration source must be ohmni_slam")
    maximum = _bounded_number(registration["maximum_residual_m"], "registration maximum_residual_m")
    inputs = {}
    for name in ("observed_tags", "known_tags"):
        item = registration[name]
        _require(
            isinstance(item, dict) and set(item) == {"path", "sha256"},
            "registration input needs path and hash",
        )
        inputs[name] = _evidence_document(
            sources.read(item["path"], item["sha256"], limit=_MAX_EVIDENCE_BYTES, name=name), name
        )
    result = register_documents(
        inputs["observed_tags"],
        inputs["known_tags"],
        held_out_tag_ids=registration["held_out_tag_ids"],
    )
    _require(
        maximum > 0 and result["max_residual_m"] <= maximum,
        "registration residual exceeds its threshold",
    )
    source = result["source"]
    _require(
        occupancy["frame"] == source["frame"]
        and occupancy["source_scope"]
        == {key: source[key] for key in ("session", "device_id", "connection_epoch", "source_id")},
        "occupancy source does not match registration",
    )
    target = result["target"]
    _require(
        target["map_id"] == manifest["map_id"]
        and target["map_version"] == manifest["bundle_version"]
        and target["physical_datum"] == frame["physical_datum"],
        "registration target does not match world pins",
    )
    return result


def _validate_tags(
    document: dict,
    geofence: tuple[list[list[float]], float, float],
    tie_ids: list[int],
    sources: _SourceSnapshots,
) -> dict[int, dict]:
    _header(document, "tags.yaml")
    _require(
        set(document) == {"schema_version", "units", "frame", "tags"},
        "tags.yaml does not match schema",
    )
    tags = document.get("tags")
    _require(
        isinstance(tags, list) and 1 <= len(tags) <= _MAX_TAGS,
        "tags must be a bounded nonempty list",
    )
    seen: dict[int, dict] = {}
    polygon, low, high = geofence
    for tag in tags:
        _require(
            isinstance(tag, dict)
            and set(tag)
            == {
                "id",
                "family",
                "floor_id",
                "size_m",
                "x_m",
                "y_m",
                "z_m",
                "yaw_rad",
                "normal",
                "T_world_tag",
                "source",
                "confidence",
                "observation_refs",
                "verified_for_flight",
                "tape_verification",
            },
            "tag does not match schema",
        )
        ident = tag["id"]
        _require(
            type(ident) is int and 0 <= ident <= 586 and ident not in seen,
            "tag IDs must be unique tag36h11 IDs",
        )
        _require(tag["family"] == "tag36h11", "tag family must be tag36h11")
        _text(tag["floor_id"], "tag floor_id")
        _require(_bounded_number(tag["size_m"], "tag size_m") > 0, "tag size_m must be positive")
        position = _point([tag[axis] for axis in ("x_m", "y_m", "z_m")], 3, "tag position")
        transform = validate_transform(tag["T_world_tag"])
        _require(
            all(abs(position[i] - transform[i][3]) <= 1e-6 for i in range(3)),
            "tag position disagrees with transform",
        )
        normal = _point(tag["normal"], 3, "tag normal")
        _require(
            all(abs(normal[i] - transform[i][2]) <= 1e-6 for i in range(3)),
            "tag normal disagrees with transform",
        )
        yaw = _bounded_number(tag["yaw_rad"], "tag yaw_rad")
        _require(
            math.hypot(transform[0][0], transform[1][0]) > 1e-6,
            "tag x axis needs a horizontal projection",
        )
        _require(
            abs(math.remainder(yaw - math.atan2(transform[1][0], transform[0][0]), 2 * math.pi))
            <= 1e-6,
            "tag yaw disagrees with transform",
        )
        _require(
            point_inside(polygon, position) and low <= position[2] <= high,
            "tag lies outside geofence",
        )
        _require(
            tag["source"] in {"measured", "surveyed", "auto_registered"},
            "tag source is unsupported",
        )
        confidence = _bounded_number(tag["confidence"], "tag confidence")
        _require(0 <= confidence <= 1, "tag confidence must be within [0,1]")
        references = tag["observation_refs"]
        _require(
            isinstance(references, list)
            and 1 <= len(references) <= _MAX_OBSERVATION_REFS
            and len(set(references)) == len(references),
            "tag observation_refs must be bounded and unique",
        )
        for reference in references:
            _text(reference, "tag observation reference")
        _require(
            type(tag["verified_for_flight"]) is bool, "tag verified_for_flight must be boolean"
        )
        if tag["verified_for_flight"]:
            _require(
                isinstance(tag["tape_verification"], dict),
                "verified tag requires tape verification",
            )
        else:
            _require(
                tag["tape_verification"] is None, "unverified tag cannot carry tape verification"
            )
        seen[ident] = tag
    origin = seen.get(0)
    _require(origin is not None, "world frame needs origin tag 0")
    _require(
        all(abs(origin[axis]) <= 1e-6 for axis in ("x_m", "y_m", "z_m"))
        and abs(math.remainder(origin["yaw_rad"], 2 * math.pi)) <= 1e-6
        and origin["normal"] == [0, 0, 1],
        "origin tag 0 disagrees with the world datum",
    )
    _require(set(tie_ids) <= set(seen), "registration tie tag is absent")
    for tag in seen.values():
        if tag["verified_for_flight"]:
            tape = tag["tape_verification"]
            _require(
                set(tape)
                == {
                    "source",
                    "evidence_path",
                    "evidence_sha256",
                    "compared_tag_id",
                    "measured_distance_m",
                    "maximum_error_m",
                },
                "tape verification does not match schema",
            )
            _require(
                tape["source"] == "independent_tape_measurement",
                "tape verification must be independent",
            )
            reference = _text(tape["evidence_path"], "tape evidence path", maximum=256)
            _require(
                reference not in tag["observation_refs"],
                "tape evidence cannot self-verify an observation",
            )
            evidence = _evidence_document(
                sources.read(
                    reference,
                    tape["evidence_sha256"],
                    limit=_MAX_EVIDENCE_BYTES,
                    name="tape evidence",
                ),
                "tape evidence",
            )
            _require(
                evidence.get("schema_version") == 1
                and evidence.get("kind") == "independent_tape_measurement",
                "tape evidence has an unsupported schema",
            )
            other_id = tape["compared_tag_id"]
            _require(
                evidence.get("tag_ids") == [tag["id"], other_id],
                "tape evidence does not identify this tag pair",
            )
            measured = _bounded_number(tape["measured_distance_m"], "tape measured_distance_m")
            bound = _bounded_number(tape["maximum_error_m"], "tape maximum_error_m")
            _require(
                _bounded_number(
                    evidence.get("measured_distance_m"), "tape evidence measured_distance_m"
                )
                == measured
                and _bounded_number(
                    evidence.get("maximum_error_m"), "tape evidence maximum_error_m"
                )
                == bound,
                "tape evidence does not match the verification",
            )
            _require(
                set(evidence)
                == {
                    "schema_version",
                    "kind",
                    "tag_ids",
                    "measured_distance_m",
                    "maximum_error_m",
                },
                "tape evidence has unexpected fields",
            )
            _require(
                type(other_id) is int and other_id in seen and other_id != tag["id"],
                "tape verification needs another known tag",
            )
            _require(
                measured > 0 and 0 < bound <= 0.10,
                "tape measurement must be positive and error bound within (0, 0.10] metres",
            )
            other = seen[other_id]
            expected = math.dist(
                (tag["x_m"], tag["y_m"], tag["z_m"]), (other["x_m"], other["y_m"], other["z_m"])
            )
            _require(abs(measured - expected) <= bound, "tape measurement exceeds its bound")
    return seen


def _validate_zones(
    document: dict,
    geofence: tuple[list[list[float]], float, float] | None = None,
    sources: _SourceSnapshots | None = None,
) -> tuple[list[list[float]], float, float]:
    _header(document, "zones.yaml")
    _require(sources is not None, "world-bundle source collector is required")
    _require(
        set(document) == {"schema_version", "units", "frame", "geofence", "zones", "corridors"},
        "zones.yaml does not match schema",
    )
    next_geofence = _volume(document.get("geofence"), "geofence")
    if geofence is not None:
        _require(next_geofence == geofence, "world-bundle geofence changed while validating")
    zones = document.get("zones")
    _require(
        isinstance(zones, list) and 1 <= len(zones) <= _MAX_ZONES,
        "zones must be a bounded nonempty list",
    )
    ids = set()
    for zone in zones:
        _require(
            isinstance(zone, dict)
            and set(zone) == {"id", "floor_id", "polygon", "z_min_m", "z_max_m"},
            "zone does not match schema",
        )
        ident = _text(zone["id"], "zone id")
        _require(ident not in ids, "duplicate zone id")
        ids.add(ident)
        _text(zone["floor_id"], "zone floor_id")
        _volume(zone, "zone")
    corridors = document.get("corridors")
    _require(
        isinstance(corridors, list) and len(corridors) <= _MAX_CORRIDORS,
        "corridors must be a bounded list",
    )
    corridor_ids = set()
    polygon, fence_low, fence_high = next_geofence
    for corridor in corridors:
        _require(
            isinstance(corridor, dict)
            and set(corridor)
            == {"id", "floor_id", "centerline", "width_m", "z_min_m", "z_max_m", "height_evidence"},
            "corridor does not match schema",
        )
        ident = _text(corridor["id"], "corridor id")
        _require(ident not in corridor_ids, "duplicate corridor id")
        corridor_ids.add(ident)
        _text(corridor["floor_id"], "corridor floor_id")
        centerline = corridor["centerline"]
        _require(
            isinstance(centerline, list) and 2 <= len(centerline) <= _MAX_CORRIDOR_POINTS,
            "corridor centerline must be bounded",
        )
        points = [_point(point, 2, "corridor centerline") for point in centerline]
        _require(
            all(point_inside(polygon, point) for point in points),
            "corridor centerline lies outside geofence",
        )
        _require(
            _bounded_number(corridor["width_m"], "corridor width_m") > 0,
            "corridor width_m must be positive",
        )
        for start, end in zip(points, points[1:], strict=False):
            _require(start != end, "corridor has a zero-length segment")
            clearance = min(
                segment_distance(start, end, a, b)
                for a, b in zip(polygon, polygon[1:], strict=False)
            )
            _require(
                clearance >= corridor["width_m"] / 2 + 1e-9,
                "corridor footprint leaves the geofence",
            )
        low = _bounded_number(corridor["z_min_m"], "corridor z_min_m")
        high = _bounded_number(corridor["z_max_m"], "corridor z_max_m")
        _require(fence_low <= low < high <= fence_high, "corridor altitude bounds exceed geofence")
        evidence = corridor["height_evidence"]
        _require(
            isinstance(evidence, list) and len(evidence) == len(points) - 1,
            "corridor needs height evidence for every segment",
        )
        for index, item in enumerate(evidence):
            _require(
                isinstance(item, dict)
                and set(item)
                == {
                    "evidence_ref",
                    "evidence_path",
                    "evidence_sha256",
                    "measured_clearance_m",
                    "maximum_flight_height_m",
                },
                "corridor height evidence does not match schema",
            )
            _text(item["evidence_ref"], "corridor height evidence reference")
            source = _evidence_document(
                sources.read(
                    item["evidence_path"],
                    item["evidence_sha256"],
                    limit=_MAX_EVIDENCE_BYTES,
                    name="corridor height evidence",
                ),
                "corridor height evidence",
            )
            clearance = _bounded_number(
                item["measured_clearance_m"], "corridor measured_clearance_m"
            )
            maximum = _bounded_number(
                item["maximum_flight_height_m"], "corridor maximum_flight_height_m"
            )
            _require(
                source
                == {
                    "schema_version": 1,
                    "kind": "manual_corridor_clearance",
                    "corridor_id": ident,
                    "segment_index": index,
                    "measured_clearance_m": clearance,
                    "maximum_flight_height_m": maximum,
                },
                "corridor height evidence does not match the segment",
            )
            _require(
                high <= maximum <= clearance, "corridor height evidence does not bound the corridor"
            )
    return next_geofence


def _validate_obstacles(document: dict) -> None:
    _header(document, "obstacles.yaml")
    _require(
        set(document) == {"schema_version", "units", "frame", "obstacles", "no_fly"},
        "obstacles.yaml does not match schema",
    )
    for key in ("obstacles", "no_fly"):
        values = document.get(key)
        _require(
            isinstance(values, list) and len(values) <= _MAX_OBSTACLES,
            f"{key} must be a bounded list",
        )
        ids = set()
        for value in values:
            _require(
                isinstance(value, dict)
                and set(value) == {"id", "floor_id", "polygon", "z_min_m", "z_max_m"},
                f"{key} item does not match schema",
            )
            ident = _text(value["id"], f"{key} id")
            _require(ident not in ids, f"duplicate {key} id")
            ids.add(ident)
            _text(value["floor_id"], f"{key} floor_id")
            _volume(value, key)


def candidate_schema_version(path: Path) -> int | None:
    """Read only the bounded manifest needed to select a bundle validator."""
    manifest = parse_document(
        _read_bounded(Path(path), "manifest.yaml", _MAX_DOCUMENT_BYTES), "manifest.yaml"
    )
    version = manifest.get("schema_version")
    return version if type(version) is int else None


def validate_candidate(path: Path) -> CandidateWorldBundle:
    """Return a validated v2 snapshot. Candidate validation never grants approval."""
    bundle = Path(path)
    try:
        documents = {name: _read_bounded(bundle, name, _MAX_DOCUMENT_BYTES) for name in _DOCUMENTS}
        manifest = parse_document(documents["manifest.yaml"], "manifest.yaml")
        sources = _SourceSnapshots(bundle)
        registration = _validate_manifest(manifest, documents, sources)
        zones = parse_document(documents["zones.yaml"], "zones.yaml")
        geofence = _validate_zones(zones, sources=sources)
        tags = parse_document(documents["tags.yaml"], "tags.yaml")
        ties = registration["fit_tag_ids"] + registration["held_out_tag_ids"]
        known_tags = _validate_tags(tags, geofence, ties, sources)
        for tie in registration["residuals"] + registration["held_out_residuals"]:
            tag = known_tags[tie["tag_id"]]
            _require(
                tie["target_xy_m"] == [tag["x_m"], tag["y_m"]],
                "registration tie disagrees with world tag",
            )
        _validate_obstacles(parse_document(documents["obstacles.yaml"], "obstacles.yaml"))
        return CandidateWorldBundle(manifest, documents, sources.bytes, registration)
    except (KeyError, TypeError, IndexError, OSError, OverflowError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"malformed or missing world-bundle data: {exc}") from exc
