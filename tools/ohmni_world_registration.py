"""Fit and validate an Ohmni-SLAM-to-world planar rigid registration."""

import argparse
import hashlib
import json
import math
from pathlib import Path

from tools.map_common import finite_number, read_document, write_document

SCHEMA_VERSION = 1
MIN_FIT_TAGS = 3
MAX_CONDITION_NUMBER = 100.0
MAX_TAG_RESIDUAL_M = 0.10
MAX_RMS_RESIDUAL_M = 0.075
MAX_HELD_OUT_RESIDUAL_M = 0.10


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    return value


def _sha256(value, name):
    _text(value, name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _point(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be two finite coordinates")
    return (finite_number(value[0], f"{name}.x"), finite_number(value[1], f"{name}.y"))


def _transform(value):
    if not isinstance(value, dict) or set(value) != {"dx_m", "dy_m", "yaw_rad"}:
        raise ValueError("transform requires dx_m, dy_m, and yaw_rad")
    return {
        "dx_m": finite_number(value["dx_m"], "dx_m"),
        "dy_m": finite_number(value["dy_m"], "dy_m"),
        "yaw_rad": finite_number(value["yaw_rad"], "yaw_rad"),
    }


def planar_transform(dx_m, dy_m, yaw_rad):
    """Create a target-from-source transform with translation in meters."""
    return _transform({"dx_m": dx_m, "dy_m": dy_m, "yaw_rad": yaw_rad})


def apply_transform(transform, point):
    """Map a source-frame point into the target frame."""
    transform = _transform(transform)
    x, y = _point(point, "point")
    cosine = math.cos(transform["yaw_rad"])
    sine = math.sin(transform["yaw_rad"])
    return (
        cosine * x - sine * y + transform["dx_m"],
        sine * x + cosine * y + transform["dy_m"],
    )


def compose_transforms(after, before):
    """Return ``after(before(point))`` for two planar rigid transforms."""
    after = _transform(after)
    before = _transform(before)
    x, y = apply_transform(after, (before["dx_m"], before["dy_m"]))
    return planar_transform(x, y, math.remainder(after["yaw_rad"] + before["yaw_rad"], 2 * math.pi))


def invert_transform(transform):
    """Return the source-from-target inverse of a planar rigid transform."""
    transform = _transform(transform)
    inverse_yaw = -transform["yaw_rad"]
    cosine = math.cos(inverse_yaw)
    sine = math.sin(inverse_yaw)
    return planar_transform(
        cosine * -transform["dx_m"] - sine * -transform["dy_m"],
        sine * -transform["dx_m"] + cosine * -transform["dy_m"],
        inverse_yaw,
    )


# Short names keep downstream registration code readable.
compose_transform = compose_transforms
invert = invert_transform
transform_point = apply_transform


def _condition_number(points, label):
    center_x = sum(point[0] for point in points) / len(points)
    center_y = sum(point[1] for point in points) / len(points)
    xx = sum((point[0] - center_x) ** 2 for point in points)
    yy = sum((point[1] - center_y) ** 2 for point in points)
    xy = sum((point[0] - center_x) * (point[1] - center_y) for point in points)
    major = (xx + yy + math.hypot(xx - yy, 2 * xy)) / 2
    minor = (xx + yy - math.hypot(xx - yy, 2 * xy)) / 2
    if major <= 1e-12 or minor <= 1e-12:
        raise ValueError(f"{label} fit tags must be noncollinear")
    condition = math.sqrt(major / minor)
    if condition > MAX_CONDITION_NUMBER:
        raise ValueError(
            f"{label} fit tags are poorly conditioned "
            f"(condition {condition:.3g} exceeds {MAX_CONDITION_NUMBER:g})"
        )
    return condition


def _orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _reject_reflection(ties):
    signs = []
    for first in range(len(ties) - 2):
        for second in range(first + 1, len(ties) - 1):
            for third in range(second + 1, len(ties)):
                source_area = _orientation(
                    ties[first]["source_xy_m"],
                    ties[second]["source_xy_m"],
                    ties[third]["source_xy_m"],
                )
                target_area = _orientation(
                    ties[first]["target_xy_m"],
                    ties[second]["target_xy_m"],
                    ties[third]["target_xy_m"],
                )
                if abs(source_area) > 1e-9 and abs(target_area) > 1e-9:
                    signs.append(source_area * target_area > 0)
    if signs and not any(signs):
        raise ValueError("fit tags imply a reflection; registration permits proper rotations only")


def fit_rigid_transform(ties):
    """Fit a proper 2D rigid transform from validated source-to-target ties."""
    if not isinstance(ties, list) or len(ties) < MIN_FIT_TAGS:
        raise ValueError(f"at least {MIN_FIT_TAGS} fit tags are required")
    source = [_point(tie["source_xy_m"], "source_xy_m") for tie in ties]
    target = [_point(tie["target_xy_m"], "target_xy_m") for tie in ties]
    _condition_number(source, "source")
    _condition_number(target, "target")
    _reject_reflection(ties)
    source_center = tuple(sum(point[index] for point in source) / len(source) for index in range(2))
    target_center = tuple(sum(point[index] for point in target) / len(target) for index in range(2))
    dot = cross = 0.0
    for source_point, target_point in zip(source, target, strict=True):
        sx, sy = source_point[0] - source_center[0], source_point[1] - source_center[1]
        tx, ty = target_point[0] - target_center[0], target_point[1] - target_center[1]
        dot += sx * tx + sy * ty
        cross += sx * ty - sy * tx
    if math.hypot(dot, cross) <= 1e-12:
        raise ValueError("fit tags cannot determine a proper rotation")
    yaw = math.atan2(cross, dot)
    rotated_center = apply_transform(planar_transform(0, 0, yaw), source_center)
    return planar_transform(
        target_center[0] - rotated_center[0], target_center[1] - rotated_center[1], yaw
    )


solve_rigid_transform = fit_rigid_transform


def _read_tag_document(document, label):
    if not isinstance(document, dict):
        raise ValueError(f"{label} document must be an object")
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError(f"{label} requires schema_version {SCHEMA_VERSION}")
    frame = _text(document.get("frame"), f"{label}.frame")
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {"name", "sha256"}:
        raise ValueError(f"{label}.provenance requires name and sha256")
    provenance = {
        "name": _text(provenance["name"], f"{label}.provenance.name"),
        "sha256": _sha256(provenance["sha256"], f"{label}.provenance.sha256"),
    }
    records = document.get("tags")
    if not isinstance(records, list):
        raise ValueError(f"{label}.tags must be a list")
    tags = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"{label} tag must be an object")
        tag_id = record.get("tag_id")
        if type(tag_id) is not int or not 0 <= tag_id <= 586:
            raise ValueError(f"{label}.tag_id must be a tag36h11 integer in 0..586")
        if tag_id in tags:
            raise ValueError(f"duplicate {label} tag_id {tag_id}")
        tags[tag_id] = _point(record.get("xy_m"), f"{label} tag {tag_id}.xy_m")
    return {"frame": frame, "provenance": provenance, "tags": tags}


def _held_out_ids(value, available):
    if value is None:
        return set()
    if not isinstance(value, (list, tuple)):
        raise ValueError("held_out_tag_ids must be a list of tag IDs")
    ids = set()
    for tag_id in value:
        if type(tag_id) is not int or tag_id not in available:
            raise ValueError("held_out_tag_ids must name matched tag IDs")
        if tag_id in ids:
            raise ValueError(f"duplicate held-out tag_id {tag_id}")
        ids.add(tag_id)
    return ids


def _residuals(ties, transform):
    result = []
    for tie in ties:
        registered = apply_transform(transform, tie["source_xy_m"])
        residual = math.dist(registered, tie["target_xy_m"])
        result.append(
            {
                "tag_id": tie["tag_id"],
                "source_xy_m": list(tie["source_xy_m"]),
                "target_xy_m": list(tie["target_xy_m"]),
                "registered_target_xy_m": list(registered),
                "residual_m": residual,
            }
        )
    return result


def register_documents(observed_document, known_document, *, held_out_tag_ids=()):
    """Build an unapproved registration candidate from independent tag documents."""
    observed = _read_tag_document(observed_document, "observed")
    known = _read_tag_document(known_document, "known")
    if observed["frame"] == known["frame"]:
        raise ValueError("observed and known frames must differ")
    if observed["provenance"]["sha256"] == known["provenance"]["sha256"]:
        raise ValueError("observed and known documents must have independent provenance")
    matched_ids = sorted(set(observed["tags"]) & set(known["tags"]))
    held_out = _held_out_ids(held_out_tag_ids, set(matched_ids))
    fit_ids = [tag_id for tag_id in matched_ids if tag_id not in held_out]
    if len(fit_ids) < MIN_FIT_TAGS:
        raise ValueError(f"at least {MIN_FIT_TAGS} matched fit tags are required")
    fit_ties = [
        {
            "tag_id": tag_id,
            "source_xy_m": observed["tags"][tag_id],
            "target_xy_m": known["tags"][tag_id],
        }
        for tag_id in fit_ids
    ]
    transform = fit_rigid_transform(fit_ties)
    residuals = _residuals(fit_ties, transform)
    max_residual = max(item["residual_m"] for item in residuals)
    rms_residual = math.sqrt(sum(item["residual_m"] ** 2 for item in residuals) / len(residuals))
    if max_residual > MAX_TAG_RESIDUAL_M:
        worst = max(residuals, key=lambda item: item["residual_m"])
        raise ValueError(
            f"outlier tag {worst['tag_id']}: residual {worst['residual_m']:.3f} m exceeds "
            f"{MAX_TAG_RESIDUAL_M:.3f} m"
        )
    if rms_residual > MAX_RMS_RESIDUAL_M:
        raise ValueError(
            f"fit RMS residual {rms_residual:.3f} m exceeds {MAX_RMS_RESIDUAL_M:.3f} m"
        )
    held_out_ties = [
        {
            "tag_id": tag_id,
            "source_xy_m": observed["tags"][tag_id],
            "target_xy_m": known["tags"][tag_id],
        }
        for tag_id in sorted(held_out)
    ]
    held_out_residuals = _residuals(held_out_ties, transform)
    if held_out_residuals:
        max_held_out = max(item["residual_m"] for item in held_out_residuals)
        if max_held_out > MAX_HELD_OUT_RESIDUAL_M:
            worst = max(held_out_residuals, key=lambda item: item["residual_m"])
            raise ValueError(
                f"held-out tag {worst['tag_id']}: residual {worst['residual_m']:.3f} m exceeds "
                f"{MAX_HELD_OUT_RESIDUAL_M:.3f} m"
            )
    else:
        max_held_out = None
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "ohmni_world_registration_candidate",
        "approval_status": "unapproved",
        "source": {"frame": observed["frame"], **observed["provenance"]},
        "target": {"frame": known["frame"], **known["provenance"]},
        "T_target_source": transform,
        "fit_tag_ids": fit_ids,
        "held_out_tag_ids": sorted(held_out),
        "residuals": residuals,
        "max_residual_m": max_residual,
        "rms_residual_m": rms_residual,
        "held_out_residuals": held_out_residuals,
        "max_held_out_residual_m": max_held_out,
    }


def _input_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observed", type=Path, help="Ohmni-local tag observation JSON")
    parser.add_argument("known", type=Path, help="independently measured world-tag JSON")
    parser.add_argument("output", type=Path, help="unapproved candidate registration JSON")
    parser.add_argument("--held-out-tag-id", type=int, action="append", default=[])
    args = parser.parse_args()
    try:
        candidate = register_documents(
            read_document(args.observed),
            read_document(args.known),
            held_out_tag_ids=args.held_out_tag_id,
        )
        candidate["input_provenance"] = {
            "observed_document_sha256": _input_sha256(args.observed),
            "known_document_sha256": _input_sha256(args.known),
        }
        write_document(args.output, candidate)
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "candidate": str(args.output),
                "approval_status": candidate["approval_status"],
                "rms_residual_m": candidate["rms_residual_m"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
