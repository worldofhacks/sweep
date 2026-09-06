"""Explicit runtime acceptance for dispatching a frozen navigation preview."""

from __future__ import annotations

from dataclasses import dataclass

from planner.navigation_artifacts import NavigationArtifact
from planner.navigation_contracts import ArtifactPin, NavigationPlan


@dataclass(frozen=True, slots=True)
class NavigationDispatchAcceptance:
    """Runtime-issued approval bound to one immutable navigation plan and artifact pins."""

    acceptance_id: str
    map_pin: ArtifactPin
    geometry_pin: ArtifactPin
    navigation_pin: ArtifactPin
    plan_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.acceptance_id, str) or not self.acceptance_id.strip():
            raise ValueError("navigation dispatch acceptance needs a nonempty identity")
        if not all(
            isinstance(pin, ArtifactPin)
            for pin in (self.map_pin, self.geometry_pin, self.navigation_pin)
        ):
            raise ValueError("navigation dispatch acceptance pins must use ArtifactPin")
        if type(self.plan_revision) is not int or self.plan_revision < 0:
            raise ValueError("navigation dispatch acceptance needs a nonnegative plan revision")

    def accept(self, plan: NavigationPlan, artifact: NavigationArtifact) -> bool:
        return (
            isinstance(plan, NavigationPlan)
            and isinstance(artifact, NavigationArtifact)
            and self.map_pin == plan.map_pin == artifact.map_pin
            and self.geometry_pin == plan.geometry_pin == artifact.geometry_pin
            and self.navigation_pin == plan.navigation_pin == artifact.navigation_pin
            and self.plan_revision == plan.plan_revision
        )
