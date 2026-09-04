"""Shared document and metric rigid-transform operations for survey tools."""

import json
import math
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


def read_document(path):
    """Read the JSON subset of YAML; reject duplicate keys and nonfinite values."""

    def invalid_constant(value):
        raise ValueError(f"nonfinite number: {value}")

    value = json.loads(
        Path(path).read_text(), object_pairs_hook=_unique_object, parse_constant=invalid_constant
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")

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
