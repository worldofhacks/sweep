"""Platform stores and composition resources close on partial startup failures."""

import sqlite3
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from relay.app import RelayRuntime, create_app
from relay.navigation_service import NavigationService
from relay.platform import PlatformServices
from relay.platform_observations import WorldObservationError
from relay.settings import RelaySettings
from relay.tests.conftest import CONSOLE_KEY


def test_invalid_observation_configuration_closes_open_navigation_store(tmp_path, monkeypatch):
    opened = []

    def navigation(*args, **kwargs):
        instance = NavigationService(*args, **kwargs)
        opened.append(instance)
        return instance

    monkeypatch.setattr("relay.platform.NavigationService", navigation)
    runtime = RelayRuntime(RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path))
    with pytest.raises(WorldObservationError, match="configuration is invalid"):
        PlatformServices(runtime, environment={"SWEEP_WORLD_OBSERVATION_SOURCES": "{"})
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0]._db.execute("SELECT 1")


@pytest.mark.parametrize("failure", ["platform", "transcript", "start", "stop", "close"])
def test_lifespan_releases_resources_even_when_startup_or_shutdown_fails(
    tmp_path, monkeypatch, failure
):
    calls = []
    platform = Mock()

    def close():
        calls.append("close")
        if failure == "close":
            raise RuntimeError("isolated close failure")

    platform.close.side_effect = close

    def build_platform(_runtime):
        if failure == "platform":
            raise RuntimeError("isolated platform failure")
        return platform

    def transcript(_runtime):
        if failure == "transcript":
            raise RuntimeError("isolated transcript failure")
        return Mock()

    async def start(_runtime):
        calls.append("start")
        if failure == "start":
            raise RuntimeError("isolated start failure")

    async def stop(_runtime):
        calls.append("stop")
        if failure == "stop":
            raise RuntimeError("isolated stop failure")

    monkeypatch.setattr(RelayRuntime, "start", start)
    monkeypatch.setattr(RelayRuntime, "stop", stop)
    app = create_app(
        RelaySettings(relay_token=CONSOLE_KEY, log_dir=tmp_path),
        platform_services_factory=build_platform,
        transcript_service_factory=transcript,
        shutdown_callback=lambda: calls.append("shutdown"),
    )
    with pytest.raises(RuntimeError, match=f"isolated {failure} failure"):
        with TestClient(app):
            pass
    assert calls[-1] == "shutdown"
    assert "stop" in calls
    assert ("close" in calls) is (failure != "platform")
