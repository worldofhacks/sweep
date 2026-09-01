"""Contract test for the frozen repository layout (PRD Appendix D, section 8.2)."""

import importlib

import pytest

PACKAGES = [
    "relay",
    "planner",
    "arbiter",
    "adapters",
    "adapters.sim",
    "adapters.crazyswarm2",
    "adapters.mavlink",
    "perception",
    "language",
    "evals",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    assert importlib.import_module(name).__name__ == name
