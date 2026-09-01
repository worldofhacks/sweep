"""Contract test for the frozen repository layout (PRD Appendix D, section 8.2)."""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

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

TOP_LEVEL = {name for name in PACKAGES if "." not in name}


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports_from_this_repo(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__file__ is not None
    assert Path(module.__file__).resolve().is_relative_to(REPO_ROOT)


def test_no_undeclared_top_level_packages() -> None:
    found = {p.parent.name for p in REPO_ROOT.glob("*/__init__.py")}
    assert found == TOP_LEVEL
