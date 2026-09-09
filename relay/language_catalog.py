"""Expose accepted destination evidence to language, never a route or motion grant."""

from __future__ import annotations

from collections.abc import Mapping

from language.contracts import ReviewCatalog, ReviewDestination, ReviewKind
from perception.object_detection import DEFAULT_TARGET_LABELS


def review_catalog(catalog: Mapping, state: Mapping, config: object | None) -> ReviewCatalog | None:
    selected = set(state.get("selection", ()))
    nodes = [node for node in state.get("drones", ()) if node.get("drone_id") in selected]
    classes = {
        "ground_vehicle"
        if node.get("node_type") == "ground"
        else node.get("device_class", "aircraft")
        for node in nodes
    }
    # A review can describe a destination before selection, but never reinterpret
    # a mixed fleet as a homogeneous execution request.
    if len(classes) > 1:
        return None
    aircraft = bool(nodes) and classes == {"aircraft"}
    search = getattr(config, "search", None)
    search_areas = set(getattr(search, "areas", ())) if aircraft else set()
    enabled = set(state.get("enabled_intent_names", ()))
    destinations = []
    all_kinds = {ReviewKind.NAVIGATE}
    for destination in catalog["destinations"]:
        if destination["excluded"] or (
            classes and not classes.intersection(destination["allowedClasses"])
        ):
            continue
        kinds = [ReviewKind.NAVIGATE]
        if aircraft and getattr(config, "navigation", None) is not None:
            kinds.append(ReviewKind.MULTIVIEW)
        if destination["zoneId"] in search_areas and "search" in enabled:
            kinds.extend((ReviewKind.SEARCH, ReviewKind.SURVEY))
        all_kinds.update(kinds)
        destinations.append(
            ReviewDestination(
                destination["zoneId"],
                destination["name"],
                tuple(destination["aliases"]),
                tuple(kinds),
            )
        )
    if not destinations:
        return None
    return ReviewCatalog(
        catalog["catalogVersion"],
        tuple(destinations),
        tuple(sorted(DEFAULT_TARGET_LABELS)) if ReviewKind.SEARCH in all_kinds else (),
        tuple(kind for kind in ReviewKind if kind in all_kinds),
    )
