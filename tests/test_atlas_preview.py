from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay.atlas import AtlasStore, NewSpace
from relay.settings import AdapterBackend
from tools import atlas_preview


def test_preview_reopens_existing_data_without_simulated_devices(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    dist = root / "console" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<title>Atlas demo</title>")
    monkeypatch.setattr(atlas_preview, "__file__", str(root / "tools" / "atlas_preview.py"))
    data = tmp_path / "saved-preview"
    store = AtlasStore(data / "atlas")
    original = store.create(
        "community-preview",
        NewSpace(
            title="Austin demo",
            latitude=30.27,
            longitude=-97.74,
        ),
    )
    store.close()
    launched = {}
    create = atlas_preview.create_app

    def configured(settings):
        assert settings.adapter_backend is AdapterBackend.REMOTE
        assert not settings.adapter_keys
        return create(settings)

    monkeypatch.setattr(atlas_preview, "create_app", configured)
    monkeypatch.setattr(
        atlas_preview.uvicorn, "run", lambda app, **kwargs: launched.update(app=app, **kwargs)
    )
    atlas_preview.main(["--data-dir", str(data), "--session-id", "community-preview", "--examples"])
    assert launched["host"] == "127.0.0.1"
    assert launched["port"] == 8177
    with TestClient(launched["app"]) as client:
        response = client.get("/relay-bootstrap.json")
        assert response.headers["cache-control"] == "no-store"
        relay = response.json()["relay"]
        assert relay["sessionId"] == "community-preview"
        assert relay["baseUrl"] == "ws://127.0.0.1:8177"
        headers = {"Authorization": "Bearer " + relay["token"]}
        spaces = client.get("/api/sessions/community-preview/atlas/spaces", headers=headers)
        assert [space["id"] for space in spaces.json()["spaces"]] == [original["space"]["id"]]
        assert client.get("/").status_code == 200
    assert Path(data / "atlas").is_dir()


@pytest.mark.parametrize("args", [["--port", "0"], ["--port", "65536"], ["--session-id", "../bad"]])
def test_preview_rejects_invalid_configuration(args):
    with pytest.raises(SystemExit) as error:
        atlas_preview.main(args)
    assert error.value.code == 2
