"""Normalize authoritative relay node classes across the two supported projections."""

from collections.abc import Mapping


def platform_device_class(row: Mapping[str, object]) -> str | None:
    current = (
        {"ground": "ground_vehicle", "aircraft": "aircraft"}.get(row.get("node_type"))
        if isinstance(row.get("node_type"), str)
        else None
    )
    legacy = row.get("device_class")
    if legacy is not None and legacy not in ("aircraft", "ground_vehicle"):
        return None
    if current is not None and legacy is not None and current != legacy:
        return None
    return current if current is not None else legacy
