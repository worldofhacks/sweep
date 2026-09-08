from pathlib import Path

import pytest

from planner.test_ground_navigation import KEY, deployment_file
from relay.autonomy import AutonomyConfig
from relay.settings import SettingsError
from relay.tests.test_autonomy import _env_example


def test_host_loads_the_same_signed_ground_deployment_as_the_node(tmp_path: Path) -> None:
    path = deployment_file(tmp_path)
    key = tmp_path / "approval.key"
    key.write_bytes(KEY)
    key.chmod(0o600)
    config = AutonomyConfig.from_env(
        {
            **_env_example(),
            "SWEEP_GROUND_NAVIGATION_CONFIG": str(path),
            "SWEEP_GROUND_NAVIGATION_KEY_FILE": str(key),
        }
    )
    assert config.ground_navigation is not None
    assert config.ground_navigation.device(9).world_pose_source_id == "world-ohmni-pose"


@pytest.mark.parametrize("missing", ["CONFIG", "KEY_FILE"])
def test_host_refuses_half_configured_ground_navigation(missing: str) -> None:
    values = {
        **_env_example(),
        "SWEEP_GROUND_NAVIGATION_CONFIG": "/unused/config.json",
        "SWEEP_GROUND_NAVIGATION_KEY_FILE": "/unused/approval.key",
    }
    del values[f"SWEEP_GROUND_NAVIGATION_{missing}"]
    with pytest.raises(SettingsError, match="requires both"):
        AutonomyConfig.from_env(values)


def test_host_refuses_a_world_readable_ground_approval_key(tmp_path: Path) -> None:
    key = tmp_path / "approval.key"
    key.write_bytes(KEY)
    key.chmod(0o644)
    with pytest.raises(SettingsError, match="mode-0600"):
        AutonomyConfig.from_env(
            {
                **_env_example(),
                "SWEEP_GROUND_NAVIGATION_CONFIG": "/unused/config.json",
                "SWEEP_GROUND_NAVIGATION_KEY_FILE": str(key),
            }
        )
