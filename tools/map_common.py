"""Shared document and metric rigid-transform operations for survey tools."""

import json
import math
import os
import stat
from pathlib import Path


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key: {key}")
        value[key] = item
    return value


def finite_number(value, name="number"):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def parse_document(payload, name="<document>"):
    """Parse the JSON subset of YAML; reject duplicate keys and nonfinite values."""

    def invalid_constant(value):
        raise ValueError(f"nonfinite number: {value}")

    value = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError(f"{name}: expected an object")

    def check(item):
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            finite_number(item)
        elif isinstance(item, dict):
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    return value


_DOCUMENTS = frozenset({"manifest.yaml", "tags.yaml", "zones.yaml", "obstacles.yaml"})


def read_document(path):
    return parse_document(Path(path).read_bytes(), str(path))


def _relative_name(name):
    if not isinstance(name, str) or not name.strip() or "\x00" in name:
        raise ValueError("source path must be nonempty text")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("unsafe source path")
    return relative


def _confined_file(bundle, relative):
    root = Path(bundle).resolve(strict=True)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("bundle paths cannot contain symlinks")
    if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
        raise ValueError("bundle path must name a confined regular file")
    return path


def bundle_document_path(bundle, name):
    """Preflight a fixed document path; reject symlinks and multiply linked files."""
    if not isinstance(name, str) or name not in _DOCUMENTS:
        raise ValueError("unknown bundle document")
    path = _confined_file(bundle, Path(name))
    if path.stat().st_nlink != 1:
        raise ValueError("bundle documents cannot have hardlink aliases")
    return path


def source_path(bundle, name):
    """Preflight a regular source file; reject traversal and document aliases."""
    relative = _relative_name(name)
    if relative.as_posix() in _DOCUMENTS:
        raise ValueError("source cannot be a bundle document")
    path = _confined_file(bundle, relative)
    for name in _DOCUMENTS:
        document = Path(bundle) / name
        if document.exists() and path.samefile(document):
            raise ValueError("source cannot alias a bundle document")
    return path


def read_bundle_bytes(bundle, name, *, source=False):
    """Read one confined snapshot using no-follow opens, including parent directories."""
    if source:
        source_path(bundle, name)
        relative = _relative_name(name)
    else:
        bundle_document_path(bundle, name)
        relative = Path(name)
    root_fd = os.open(Path(bundle).resolve(strict=True), os.O_RDONLY | os.O_DIRECTORY)
    directory_fd = root_fd
    file_fd = None
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            if directory_fd != root_fd:
                os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("bundle path must name a regular file")
        if not source and info.st_nlink != 1:
            raise ValueError("bundle documents cannot have hardlink aliases")
        if source:
            for document in _DOCUMENTS:
                try:
                    reserved = os.stat(document, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino) == (reserved.st_dev, reserved.st_ino):
                    raise ValueError("source cannot alias a bundle document")
        with os.fdopen(file_fd, "rb") as handle:
            file_fd = None
            return handle.read()
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd != root_fd:
            os.close(directory_fd)
        os.close(root_fd)


def write_document(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def validate_transform(value):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("transform must be a 4x4 matrix")
    matrix = []
    for row in value:
        if not isinstance(row, list) or len(row) != 4:
            raise ValueError("transform must be a 4x4 matrix")
        matrix.append([finite_number(item, "transform element") for item in row])
    if any(abs(matrix[3][i] - expected) > 1e-6 for i, expected in enumerate([0, 0, 0, 1])):
        raise ValueError("transform must have homogeneous last row [0,0,0,1]")
    for i in range(3):
        for j in range(3):
            dot = sum(matrix[k][i] * matrix[k][j] for k in range(3))
            if abs(dot - (i == j)) > 1e-6:
                raise ValueError("transform rotation must be orthonormal")
    a, b, c = [row[:3] for row in matrix[:3]]
    determinant = (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )
    if abs(determinant - 1) > 1e-6:
        raise ValueError("transform rotation must be proper (determinant +1)")
    return matrix


def transform_point(transform, point):
    matrix = validate_transform(transform)
    if not isinstance(point, list) or len(point) != 3:
        raise ValueError("point must have three coordinates")
    point = [finite_number(item, "point coordinate") for item in point]
    return [sum(row[i] * point[i] for i in range(3)) + row[3] for row in matrix[:3]]
