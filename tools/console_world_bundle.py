"""Bounded, immutable world bundles for shared map authoring (#81).

Validation establishes internal consistency of supplied evidence. It cannot prove
that a tape measurement, registration, or photograph was physically authentic.
Occupancy pixels never establish flight clearance; corridor heights stay explicit.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import struct
import unicodedata
from collections.abc import Mapping
from typing import Any

from tools.map_validate import _inside, _intersects, _polygon

MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_777_216
MAX_COORDINATE_M = 1_000_000
MAX_GEOMETRY_WORK = 2_000_000
MAX_TIMESTAMP = 2**53 - 1
EPS = 1e-9
VALIDATOR_VERSION = "world-bundle-v1.1"
DOCUMENTS = ("image", "zones", "corridors", "obstacles", "geofence", "tags")


class BundleError(ValueError):
    pass


def canonical_json(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise BundleError("world document exceeds 16 MiB")
        return encoded
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise BundleError(f"invalid bounded JSON document: {error}") from error


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BundleError(message)


def _object(value: object, fields: set[str], name: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == fields, f"{name} fields are invalid")
    return value  # type: ignore[return-value]


def _text(value: object, name: str, *, maximum: int = 4096) -> str:
    _require(
        isinstance(value, str)
        and 0 < len(value) <= maximum
        and value == value.strip()
        and value.isprintable(),
        f"{name} must be bounded, nonempty normalized text",
    )
    return value  # type: ignore[return-value]


def _spatial_identifier(value: object, name: str) -> str:
    text = _text(value, name, maximum=128)
    _require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", text) is not None,
        f"{name} must use the shared spatial identifier syntax",
    )
    return text


def _number(value: object, name: str, *, positive: bool = False) -> float:
    _require(
        type(value) in (int, float)
        and math.isfinite(value)
        and abs(value) <= MAX_COORDINATE_M
        and (not positive or value > 0),
        f"{name} must be finite bounded metres" + (" and positive" if positive else ""),
    )
    return float(value)  # type: ignore[arg-type]


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    _require(type(value) is int and minimum <= value <= MAX_TIMESTAMP, f"invalid {name}")
    return value  # type: ignore[return-value]


def _array(value: object, name: str, maximum: int) -> list[Any]:
    _require(isinstance(value, list) and len(value) <= maximum, f"invalid bounded {name}")
    return value  # type: ignore[return-value]


def _point(value: object) -> list[float]:
    point = _object(value, {"x", "y"}, "point")
    return [_number(point["x"], "point.x"), _number(point["y"], "point.y")]


def _geometry(raw: object, closed: bool) -> list[list[float]]:
    points = [_point(p) for p in _array(raw, "points", 512)]
    if closed:
        return _polygon(points)
    _require(len(points) >= 2, "corridor needs two points")
    _require(len({tuple(p) for p in points}) == len(points), "corridor repeats a vertex")
    for a, b, c in zip(points, points[1:], points[2:], strict=False):
        _require(
            not (
                abs(_cross(a, b, c)) <= EPS
                and (a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1]) > 0
            ),
            "corridor doubles back over an adjacent segment",
        )
    for i, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        for c, d in zip(points[i + 2 :], points[i + 3 :], strict=False):
            _require(not _intersects(a, b, c, d), "corridor self-intersects")
    return points


def _cross(a: list[float], b: list[float], c: list[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_distance(p: list[float], a: list[float], b: list[float]) -> float:
    length = sum((b[i] - a[i]) ** 2 for i in range(2))
    t = max(0.0, min(1.0, sum((p[i] - a[i]) * (b[i] - a[i]) for i in range(2)) / length))
    return math.hypot(*(p[i] - a[i] - t * (b[i] - a[i]) for i in range(2)))


def _segment_distance(a: list[float], b: list[float], c: list[float], d: list[float]) -> float:
    if _intersects(a, b, c, d):
        return 0.0
    return min(
        _point_distance(a, c, d),
        _point_distance(b, c, d),
        _point_distance(c, a, b),
        _point_distance(d, a, b),
    )


def _segment_inside(a: list[float], b: list[float], boundary: list[list[float]]) -> bool:
    """Check every interval between boundary crossings, including vertex crossings."""
    if not _inside(boundary, a) or not _inside(boundary, b):
        return False
    cuts = {0.0, 1.0}
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    for c, d in zip(boundary, boundary[1:], strict=False):
        ex, ey = d[0] - c[0], d[1] - c[1]
        denominator = dx * ey - dy * ex
        if abs(denominator) > EPS:
            t = ((c[0] - a[0]) * ey - (c[1] - a[1]) * ex) / denominator
            u = ((c[0] - a[0]) * dy - (c[1] - a[1]) * dx) / denominator
            if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
                cuts.add(max(0.0, min(1.0, t)))
        elif abs(_cross(a, b, c)) <= EPS:
            for p in (c, d):
                t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length
                if 0 < t < 1:
                    cuts.add(t)
    ordered = sorted(cuts)
    return all(
        _inside(boundary, [a[0] + (lo + hi) / 2 * dx, a[1] + (lo + hi) / 2 * dy])
        for lo, hi in zip(ordered, ordered[1:], strict=False)
    )


def _within(points: list[list[float]], boundary: list[list[float]], margin: float = 0) -> bool:
    for a, b in zip(points, points[1:], strict=False):
        if not _segment_inside(a, b, boundary):
            return False
        if margin and any(
            _segment_distance(a, b, c, d) + EPS < margin
            for c, d in zip(boundary, boundary[1:], strict=False)
        ):
            return False
    return True


def _collides(points: list[list[float]], obstacle: list[list[float]], margin: float = 0) -> bool:
    if any(_inside(obstacle, p) for p in points):
        return True
    if points[0] == points[-1] and _inside(points, obstacle[0]):
        return True
    return any(
        _segment_distance(a, b, c, d) <= margin + EPS
        for a, b in zip(points, points[1:], strict=False)
        for c, d in zip(obstacle, obstacle[1:], strict=False)
    )


def _image_size(payload: bytes, mime: str) -> tuple[int, int]:
    """Bound image allocation from actual headers before invoking the decoder."""
    if mime == "png":
        _require(
            payload[:8] == b"\x89PNG\r\n\x1a\n"
            and len(payload) >= 33
            and payload[12:16] == b"IHDR"
            and payload[8:12] == b"\0\0\0\r",
            "invalid PNG header",
        )
        return struct.unpack(">II", payload[16:24])
    _require(payload[:2] == b"\xff\xd8", "invalid JPEG header")
    offset = 2
    while offset < len(payload):
        _require(payload[offset] == 0xFF, "invalid JPEG marker")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        _require(offset < len(payload), "truncated JPEG marker")
        marker = payload[offset]
        offset += 1
        if marker in {0xD9, 0xDA}:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        _require(offset + 2 <= len(payload), "truncated JPEG segment")
        length = int.from_bytes(payload[offset : offset + 2], "big")
        _require(length >= 2 and offset + length <= len(payload), "invalid JPEG segment")
        if marker in {0xC0, 0xC1, 0xC2}:
            _require(length >= 8, "invalid JPEG dimensions")
            height, width = struct.unpack(">HH", payload[offset + 3 : offset + 7])
            return width, height
        offset += length
    raise BundleError("JPEG has no supported dimension header")


def validate_image(value: object, *, occupancy_only: bool = False) -> dict[str, Any]:
    image = _object(value, {"name", "dataUrl", "width", "height", "sha256"}, "image")
    _text(image["name"], "image.name", maximum=256)
    data_url = image["dataUrl"]
    _require(
        isinstance(data_url, str) and len(data_url) <= MAX_IMAGE_BYTES * 1.4, "image exceeds 8 MiB"
    )
    mime = next(
        (kind for kind in ("png", "jpeg") if data_url.startswith(f"data:image/{kind};base64,")),
        None,
    )
    _require(mime is not None, "image must be base64 PNG or JPEG")
    try:
        payload = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    except (binascii.Error, ValueError) as error:
        raise BundleError("invalid image base64") from error
    _require(len(payload) <= MAX_IMAGE_BYTES, "image exceeds 8 MiB")
    _require(hashlib.sha256(payload).hexdigest() == image["sha256"], "image hash mismatch")
    width, height = _image_size(payload, mime)  # type: ignore[arg-type]
    _require(
        0 < width and 0 < height and width * height <= MAX_PIXELS,
        "image dimensions exceed the 16 megapixel bound",
    )
    _require(
        type(image["width"]) is int
        and type(image["height"]) is int
        and (width, height) == (image["width"], image["height"]),
        "image dimensions do not match its bytes",
    )
    if occupancy_only:
        from tools.occupancy_png import decode_occupancy_png

        rows = decode_occupancy_png(payload)
        _require(len(rows) == height and len(rows[0]) == width, "occupancy PNG dimensions differ")
        return image

    import cv2
    import numpy as np

    try:
        decoded = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    except cv2.error as error:
        raise BundleError("occupancy image cannot be decoded") from error
    _require(
        decoded is not None and decoded.shape[:2] == (height, width),
        "occupancy image cannot be decoded",
    )
    return image


METADATA_FIELDS = {
    "mapVersion",
    "floorId",
    "frame",
    "resolutionM",
    "originXM",
    "originYM",
    "units",
    "createdAt",
    "creationEvidence",
    "registration",
}
FEATURE_FIELDS = {
    "id",
    "kind",
    "name",
    "aliases",
    "points",
    "widthM",
    "flightHeightM",
    "heightToleranceM",
    "heightEvidence",
}
TAG_FIELDS = {
    "id",
    "tagId",
    "family",
    "sizeM",
    "position",
    "heightM",
    "yawRad",
    "source",
    "confidence",
    "observations",
    "usedForFlight",
    "tapeVerified",
    "tapeEvidence",
}


def validate_draft(value: object, *, occupancy_only: bool = False) -> list[dict[str, str]]:
    """Return bounded refusal details; incomplete drafts remain editable in the store."""
    issues: list[dict[str, str]] = []

    def add(path: str, error: object) -> None:
        if len(issues) < 256:
            issues.append({"path": path, "message": str(error)[:2048]})

    try:
        canonical_json(value)
        draft = _object(value, {"format", "metadata", "image", "features", "tags"}, "draft")
        _require(draft["format"] == "sweep-map-draft-v1", "unsupported draft format")
        features = _array(draft["features"], "features", 256)
        tags = _array(draft["tags"], "tags", 512)
        counts = [
            (f.get("kind"), len(f["points"]))
            for f in features
            if isinstance(f, dict) and isinstance(f.get("points"), list)
        ]
        total = sum(count for _, count in counts)
        fence = sum(count for kind, count in counts if kind == "geofence")
        forbidden = sum(count for kind, count in counts if kind in ("obstacle", "no_fly"))
        work = sum(count * count for _, count in counts) + 2 * total * fence + 4 * total * forbidden
        _require(
            total <= 4096 and work <= MAX_GEOMETRY_WORK,
            "geometry exceeds the bounded validation computation budget",
        )
        _require(
            sum(kind in ("zone", "obstacle", "no_fly") for kind, _ in counts) <= 128,
            "named destinations exceed the shared catalog bound of 128",
        )
    except (ValueError, TypeError, OverflowError) as error:
        return [{"path": "draft", "message": str(error)[:2048]}]
    metadata, image = None, None
    try:
        metadata = _object(draft["metadata"], METADATA_FIELDS, "metadata")
        _spatial_identifier(metadata["mapVersion"], "map version")
        _spatial_identifier(metadata["floorId"], "floor")
        _require(metadata["frame"] == "world", "frame must be explicitly world")
        _require(metadata["units"] == "m", "units must be m")
        _integer(metadata["createdAt"], "creation timestamp")
        _text(metadata["creationEvidence"], "creation evidence")
        _number(metadata["resolutionM"], "resolution", positive=True)
        _number(metadata["originXM"], "origin x")
        _number(metadata["originYM"], "origin y")
        registration = _object(
            metadata["registration"],
            {"sourceFrame", "transformId", "residualM", "thresholdM", "evidence"},
            "registration",
        )
        _text(registration["sourceFrame"], "registration source frame", maximum=256)
        _require(
            registration["sourceFrame"] != "world", "registration needs a distinct source frame"
        )
        _text(registration["transformId"], "registration transform identity", maximum=256)
        residual = _number(registration["residualM"], "registration residual")
        threshold = _number(registration["thresholdM"], "registration threshold", positive=True)
        _require(0 <= residual <= threshold, "registration residual exceeds threshold")
        _text(registration["evidence"], "registration evidence")
    except (ValueError, TypeError, OverflowError) as error:
        add("metadata", error)
        metadata = None
    try:
        image = validate_image(draft["image"], occupancy_only=occupancy_only)
    except (ValueError, TypeError, OverflowError) as error:
        add("image", error)
    ids: set[str] = set()
    names: set[str] = set()
    geometries: list[tuple[dict[str, Any], list[list[float]]]] = []
    for index, raw in enumerate(features):
        try:
            feature = _object(raw, FEATURE_FIELDS, "feature")
            identifier = _spatial_identifier(feature["id"], "feature id")
            _require(identifier not in ids, "duplicate object id")
            ids.add(identifier)
            _require(
                feature["kind"] in {"zone", "geofence", "no_fly", "obstacle", "corridor"},
                "unknown feature kind",
            )
            for label in [feature["name"], *_array(feature["aliases"], "aliases", 16)]:
                label = _text(label, "name or alias", maximum=128)
                _require(
                    len(label.encode("utf-16-le")) // 2 <= 128,
                    "name or alias exceeds the shared display bound",
                )
                name = " ".join(unicodedata.normalize("NFKC", label).split()).casefold()
                _require(name not in names, "duplicate name or alias")
                names.add(name)
            points = _geometry(feature["points"], feature["kind"] != "corridor")
            if feature["kind"] == "corridor":
                _number(feature["widthM"], "corridor width", positive=True)
                height = _number(feature["flightHeightM"], "flight height", positive=True)
                tolerance = _number(feature["heightToleranceM"], "height tolerance", positive=True)
                _require(height > tolerance, "flight height lower bound must remain above ground")
                _text(feature["heightEvidence"], "hand-measured height evidence")
            else:
                _require(
                    all(
                        feature[key] is None
                        for key in ("widthM", "flightHeightM", "heightToleranceM")
                    )
                    and feature["heightEvidence"] == "",
                    "non-corridor height fields must be empty",
                )
            geometries.append((feature, points))
        except (ValueError, TypeError, OverflowError) as error:
            add(f"features.{index}", error)
    fences = [points for feature, points in geometries if feature["kind"] == "geofence"]
    if len(fences) != 1:
        add("geofence", "exactly one valid geofence is required")
    if not any(feature["kind"] == "zone" for feature, _ in geometries):
        add("zones", "at least one valid named zone is required")
    obstacles = [
        points for feature, points in geometries if feature["kind"] in {"no_fly", "obstacle"}
    ]
    for feature, points in geometries:
        path = f"features.{feature['id']}"
        margin = feature["widthM"] / 2 if feature["kind"] == "corridor" else 0
        if (
            len(fences) == 1
            and feature["kind"] != "geofence"
            and not _within(points, fences[0], margin)
        ):
            add(path, "geometry including corridor width leaves the geofence")
        if feature["kind"] in {"zone", "corridor"} and any(
            _collides(points, obstacle, margin) for obstacle in obstacles
        ):
            add(path, "approved geometry intersects a static obstacle or no-fly area")
        if metadata and image:
            x, y, resolution = metadata["originXM"], metadata["originYM"], metadata["resolutionM"]
            if any(
                not (
                    x <= p[0] <= x + image["width"] * resolution
                    and y <= p[1] <= y + image["height"] * resolution
                )
                for p in points
            ):
                add(path, "geometry leaves the saved image extent")
    tag_ids: set[int] = set()
    for index, raw in enumerate(tags):
        try:
            tag = _object(raw, TAG_FIELDS, "tag")
            identifier = _spatial_identifier(tag["id"], "tag object id")
            _require(identifier not in ids, "duplicate object id")
            ids.add(identifier)
            tag_id = _integer(tag["tagId"], "tag id")
            _require(tag_id not in tag_ids, "duplicate tag id")
            tag_ids.add(tag_id)
            _text(tag["family"], "tag family", maximum=256)
            _number(tag["sizeM"], "tag size", positive=True)
            _number(tag["heightM"], "tag height")
            position = _point(tag["position"])
            if tag["yawRad"] is not None:
                _require(abs(_number(tag["yawRad"], "tag yaw")) <= math.pi, "tag yaw outside +/-pi")
            _require(
                tag["source"] in {"measured", "surveyed", "auto_registered"}, "unknown tag source"
            )
            _require(
                0 <= _number(tag["confidence"], "tag confidence") <= 1, "invalid tag confidence"
            )
            observations = _array(tag["observations"], "tag observations", 128)
            for observation in observations:
                _text(observation, "observation reference", maximum=512)
            _require(len(set(observations)) == len(observations), "duplicate observation reference")
            _require(
                type(tag["usedForFlight"]) is bool and type(tag["tapeVerified"]) is bool,
                "tag verification flags must be booleans",
            )
            _require(
                isinstance(tag["tapeEvidence"], str) and len(tag["tapeEvidence"]) <= 4096,
                "invalid tape evidence",
            )
            if tag["tapeVerified"] or tag["usedForFlight"]:
                _require(
                    tag["tapeVerified"] and observations and tag["yawRad"] is not None,
                    "flight verification requires tape check, observations and tag orientation",
                )
                _text(tag["tapeEvidence"], "tape verification evidence")
            if len(fences) == 1:
                _require(_inside(fences[0], position), "tag leaves the geofence")
        except (ValueError, TypeError, OverflowError) as error:
            add(f"tags.{index}", error)
    return issues


def _segments(feature: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "start": a,
            "end": b,
            **{
                key: feature[key]
                for key in ("widthM", "flightHeightM", "heightToleranceM", "heightEvidence")
            },
        }
        for a, b in zip(feature["points"], feature["points"][1:], strict=False)
    ]


def build_world_bundle(draft: dict[str, Any]) -> dict[str, Any]:
    issues = validate_draft(draft)
    if issues:
        raise BundleError(f"draft cannot become an approved bundle: {issues[0]['message']}")
    draft = json.loads(canonical_json(draft))
    metadata = draft["metadata"]
    bundle: dict[str, Any] = {
        "format": "sweep-world-bundle-v1",
        "manifest": {
            "schemaVersion": 1,
            **{
                key: metadata[key]
                for key in (
                    "mapVersion",
                    "floorId",
                    "frame",
                    "units",
                    "createdAt",
                    "creationEvidence",
                    "registration",
                )
            },
            "image": {
                **{key: draft["image"][key] for key in ("name", "width", "height", "sha256")},
                **{key: metadata[key] for key in ("resolutionM", "originXM", "originYM")},
            },
        },
        "image": draft["image"],
        "zones": [f for f in draft["features"] if f["kind"] == "zone"],
        "corridors": [
            {**f, "segments": _segments(f)} for f in draft["features"] if f["kind"] == "corridor"
        ],
        "obstacles": [f for f in draft["features"] if f["kind"] in {"obstacle", "no_fly"}],
        "geofence": next(f for f in draft["features"] if f["kind"] == "geofence"),
        "tags": [
            {
                **tag,
                "verifiedForFlight": tag["tapeVerified"]
                and bool(tag["observations"])
                and bool(tag["tapeEvidence"])
                and tag["yawRad"] is not None,
            }
            for tag in draft["tags"]
        ],
        "derivedArtifacts": [],
    }
    bundle["manifest"]["documentHashes"] = {key: content_hash(bundle[key]) for key in DOCUMENTS}
    bundle["manifest"]["contentHash"] = world_bundle_hash(bundle)
    return bundle


def world_bundle_hash(bundle: Mapping[str, Any], *, static_only: bool = False) -> str:
    body = {
        **bundle,
        "manifest": {
            key: value for key, value in bundle["manifest"].items() if key != "contentHash"
        },
    }
    if static_only:
        body["derivedArtifacts"] = []
    return content_hash(body)


def validate_world_bundle(value: object, *, occupancy_only: bool = False) -> list[dict[str, str]]:
    """Validate the independent #81 publication schema, hashes and derived bindings."""
    try:
        canonical_json(value)
        bundle = _object(value, {"format", "manifest", "derivedArtifacts", *DOCUMENTS}, "bundle")
        _require(bundle["format"] == "sweep-world-bundle-v1", "unsupported world bundle format")
        manifest = _object(
            bundle["manifest"],
            {
                "schemaVersion",
                "mapVersion",
                "floorId",
                "frame",
                "units",
                "createdAt",
                "creationEvidence",
                "registration",
                "image",
                "documentHashes",
                "contentHash",
            },
            "manifest",
        )
        _require(
            type(manifest["schemaVersion"]) is int and manifest["schemaVersion"] == 1,
            "unsupported world schema version",
        )
        _require(manifest["contentHash"] == world_bundle_hash(bundle), "world bundle hash mismatch")
        _require(
            manifest["documentHashes"] == {key: content_hash(bundle[key]) for key in DOCUMENTS},
            "world document hash mismatch",
        )
        image_metadata = _object(
            manifest["image"],
            {"name", "width", "height", "sha256", "resolutionM", "originXM", "originYM"},
            "manifest image",
        )
        _require(
            all(
                image_metadata[key] == bundle["image"][key]
                for key in ("name", "width", "height", "sha256")
            ),
            "manifest image mismatch",
        )
        features = [
            bundle["geofence"],
            *_array(bundle["zones"], "zones", 256),
            *_array(bundle["obstacles"], "obstacles", 256),
        ]
        _require(bundle["geofence"]["kind"] == "geofence", "invalid geofence kind")
        _require(all(f["kind"] == "zone" for f in bundle["zones"]), "invalid zone kind")
        _require(
            all(f["kind"] in {"no_fly", "obstacle"} for f in bundle["obstacles"]),
            "invalid obstacle kind",
        )
        for raw in _array(bundle["corridors"], "corridors", 256):
            corridor = _object(raw, FEATURE_FIELDS | {"segments"}, "corridor")
            _require(
                corridor["kind"] == "corridor" and corridor["segments"] == _segments(corridor),
                "corridor segment height binding mismatch",
            )
            features.append({key: item for key, item in corridor.items() if key != "segments"})
        tags = []
        for raw in _array(bundle["tags"], "tags", 512):
            tag = _object(raw, TAG_FIELDS | {"verifiedForFlight"}, "published tag")
            verified = (
                tag["tapeVerified"] and bool(tag["observations"]) and bool(tag["tapeEvidence"])
            )
            verified = bool(verified and tag["yawRad"] is not None)
            _require(
                type(tag["verifiedForFlight"]) is bool and tag["verifiedForFlight"] == verified,
                "flight tag verification binding mismatch",
            )
            tags.append({key: item for key, item in tag.items() if key != "verifiedForFlight"})
        metadata = {
            key: manifest[key]
            for key in (
                "mapVersion",
                "floorId",
                "frame",
                "units",
                "createdAt",
                "creationEvidence",
                "registration",
            )
        }
        metadata.update(
            {key: image_metadata[key] for key in ("resolutionM", "originXM", "originYM")}
        )
        issues = validate_draft(
            {
                "format": "sweep-map-draft-v1",
                "metadata": metadata,
                "image": bundle["image"],
                "features": features,
                "tags": tags,
            },
            occupancy_only=occupancy_only,
        )
        _require(not issues, issues[0]["message"] if issues else "invalid draft")
        artifact_ids = set()
        for raw in _array(bundle["derivedArtifacts"], "derived artifacts", 256):
            artifact = _object(
                raw,
                {
                    "artifactId",
                    "kind",
                    "frame",
                    "mapVersion",
                    "sourceHash",
                    "contentHash",
                    "content",
                },
                "derived artifact",
            )
            identifier = _text(artifact["artifactId"], "artifact id", maximum=256)
            _require(identifier not in artifact_ids, "duplicate derived artifact id")
            artifact_ids.add(identifier)
            _text(artifact["kind"], "derived artifact kind", maximum=256)
            _require(
                artifact["frame"] == "world"
                and artifact["mapVersion"] == manifest["mapVersion"]
                and artifact["sourceHash"] == world_bundle_hash(bundle, static_only=True),
                "derived artifact is bound to another frame, map version, or bundle",
            )
            _require(
                artifact["contentHash"] == content_hash(artifact["content"]),
                "derived artifact content hash mismatch",
            )
        return []
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as error:
        return [{"path": "bundle", "message": str(error)[:2048]}]
