"""Account contributions keep provenance without granting paid/operator authority."""

import json
import uuid
from io import BytesIO

import pytest
from PIL import Image

from relay.atlas import AtlasError, AtlasStore
from relay.memory_store import MemoryAsset, MemoryNotes, ReviewMemory, SaveMemory
from relay.tests.test_atlas_accounts import OWNER, PNG, SPACE, create_space, join, signed, upload
from relay.tests.test_atlas_accounts import api as api
from relay.tests.test_atlas_accounts import keys as keys
from relay.tests.test_memory_context import wav_bytes


def setup_memory(api, keys):
    url, created = create_space(api)
    contributor = signed(keys)
    join(api, url, contributor)
    capture = upload(api, url, contributor).json()
    return url, created, contributor, capture, url + f"/captures/{capture['id']}/memory"


def attach(api, base, auth):
    return api.post(
        base + "/assets",
        content=wav_bytes(),
        headers={
            **auth,
            "Content-Type": "audio/wav",
            "X-Sweep-Memory-Asset": json.dumps(
                {"title": "Birds in the garden", "role": "ambient", "rights_confirmed": True}
            ),
        },
    )


def test_contributor_story_recording_review_and_shared_revisit(api, keys, monkeypatch):
    url, _, auth, capture, base = setup_memory(api, keys)
    viewer = signed(keys, "friend")
    join(api, url, viewer, "viewer")
    provider = []
    monkeypatch.setattr("relay.memory_context.bounded_json", lambda *a, **kw: provider.append(a))
    value = api.get(base, headers=auth).json()
    assert value["can_edit"] and not value["can_analyze"]
    assert api.post(base + "/inspect", headers=auth).status_code == 200
    notes = {"description": "We planted the first seeds together.", "feeling": "Joyful"}
    saved = api.post(base, headers=auth, json={"revision": 0, "notes": notes})
    assert saved.status_code == 200, saved.text
    assert saved.json()["last_edit"]["actor"] == capture["account_id"]
    asset = attach(api, base, auth)
    assert asset.status_code == 201, asset.text
    assert asset.json()["added_by"] == capture["account_id"]
    assert attach(api, base, auth).json() == asset.json()
    reviewed = api.post(base + "/review", headers=auth, json={"revision": 2})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review"]["actor"] == capture["account_id"]
    assert api.post(base + "/review", headers=auth, json={"revision": 2}).status_code == 200
    seen = api.get(base, headers=viewer).json()
    assert not seen["can_edit"] and not seen["can_analyze"]
    assert seen["notes"]["description"] == notes["description"]
    assert (
        api.get(base + f"/assets/{asset.json()['id']}/media", headers=viewer).content == wav_bytes()
    )
    history = api.get(base + "/history", headers=viewer)
    assert history.headers["cache-control"] == "no-store"
    assert [e["kind"] for e in history.json()["history"]] == ["review", "recording", "notes"]
    assert history.json()["history"][-1]["previous"]["description"] == ""
    assert api.get(base.removesuffix("/memory") + "/media", headers=auth).content == PNG
    assert seen["capture"] == capture
    assert not provider
    viewer_id = api.post("/api/atlas/account", headers=viewer).json()["account"]["id"]
    assert api.delete(url + "/members/" + viewer_id, headers=OWNER).status_code == 200
    assert api.get(base + f"/assets/{asset.json()['id']}/media", headers=viewer).status_code == 403
    assert api.get(base + "/history", headers=viewer).status_code == 403
    assert (
        api.post(base + "/analyze", headers=auth, json={"revision": 2, "ai": True}).status_code
        == 401
    )


def test_owner_can_help_but_other_contributors_and_viewers_cannot_edit(api, keys):
    owner, author = signed(keys, "owner"), signed(keys, "author")
    grant = api.post(
        "/api/atlas/account/spaces",
        headers=owner,
        json={"draft_id": str(uuid.uuid4()), "space": SPACE},
    ).json()
    url = f"/api/sessions/{grant['session']}/atlas/spaces/{grant['space_id']}"
    join(api, url, author)
    capture = upload(api, url, author).json()
    base = url + f"/captures/{capture['id']}/memory"
    for role in ("contributor", "viewer"):
        other = signed(keys, role)
        join(api, url, other, role)
        assert not api.get(base, headers=other).json()["can_edit"]
        for suffix in ("", "/inspect", "/review", "/assets"):
            assert api.post(base + suffix, headers=other, json={}).status_code == 403
    assert (
        api.post(
            base, headers=owner, json={"revision": 0, "notes": {"feeling": "Peaceful"}}
        ).status_code
        == 200
    )
    assert api.post(base + "/analyze", headers=owner, json={"revision": 1}).status_code == 401
    assert (
        api.post(
            base, headers=author, json={"revision": 0, "notes": {"feeling": "Joyful"}}
        ).status_code
        == 409
    )
    assert len(api.get(base + "/history", headers=owner).json()["history"]) == 1
    assert api.get(base, headers=author).json()["notes"]["feeling"] == "Peaceful"
    stranger = signed(keys, "stranger")
    assert api.get(base + "/history", headers=stranger).status_code == 403


def test_revocation_during_upload_or_inspection_rechecks_commit(api, keys, monkeypatch):
    url, created, auth, capture, base = setup_memory(api, keys)
    store = api.app.state.atlas_store

    def revoke():
        assert (
            api.delete(url + "/members/" + capture["account_id"], headers=OWNER).status_code == 200
        )

    from relay.memory_routes import admitted_asset

    def revoked_asset(path, mime):
        result = admitted_asset(path, mime)
        revoke()
        return result

    monkeypatch.setattr("relay.memory_routes.admitted_asset", revoked_asset)
    assert attach(api, base, auth).status_code == 403
    assert not list(store.memories.media.iterdir())
    assert not list(store.root.glob("memory-upload-*"))
    for suffix in ("", "/history"):
        assert api.get(base + suffix, headers=auth).status_code == 403
    for operation in (
        lambda: store.memories.save(
            created["space"]["id"],
            capture["id"],
            SaveMemory(revision=0, notes=MemoryNotes()),
            account_id=capture["account_id"],
        ),
        lambda: store.memories.review(
            created["space"]["id"],
            capture["id"],
            ReviewMemory(revision=0),
            account_id=capture["account_id"],
        ),
    ):
        with pytest.raises(AtlasError) as denied:
            operation()
        assert denied.value.status == 403
    join(api, url, auth)

    def revoked_inspection(*args):
        revoke()
        return {"mime": "image/png", "timestamp": None}

    monkeypatch.setattr("relay.memory_routes.inspect_media", revoked_inspection)
    assert api.post(base + "/inspect", headers=auth).status_code == 403
    assert store.memories.get(created["space"]["id"], capture["id"])["inspection"] is None


def test_history_persists_and_limit_rolls_back_recording(api, keys, tmp_path):
    _, created, auth, capture, base = setup_memory(api, keys)
    store = api.app.state.atlas_store
    space, cid = created["space"]["id"], capture["id"]
    assert (
        api.post(
            base, headers=auth, json={"revision": 0, "notes": {"description": "Original story"}}
        ).status_code
        == 200
    )
    reopened = AtlasStore(store.root)
    try:
        assert reopened.memories.history(space, cid)[0]["notes"]["description"] == "Original story"
    finally:
        reopened.close()
    with store.lock, store.db:
        store.db.execute(
            "UPDATE memory_edits SET sequence=200 WHERE space=? AND capture=?", (space, cid)
        )
    staged = tmp_path / "recording.wav"
    staged.write_bytes(wav_bytes())
    with pytest.raises(AtlasError) as limit:
        store.memories.add_asset(
            space,
            cid,
            staged,
            "audio/wav",
            MemoryAsset(title="Birds", role="ambient", rights_confirmed=True),
            account_id=capture["account_id"],
        )
    assert limit.value.status == 429
    assert not list(store.memories.media.iterdir())
    value = store.memories.get(space, cid)
    assert value["revision"] == 1 and not value["assets"]


def test_history_pages_remain_bounded_and_revocable(api, keys):
    url, _, auth, capture, base = setup_memory(api, keys)
    for revision in range(23):
        assert (
            api.post(
                base,
                headers=auth,
                json={"revision": revision, "notes": {"description": f"Story {revision}"}},
            ).status_code
            == 200
        )
    latest = api.get(base + "/history", headers=auth).json()["history"]
    assert [entry["sequence"] for entry in latest] == list(range(23, 3, -1))
    earlier = api.get(base + "/history/4", headers=auth).json()["history"]
    assert [entry["sequence"] for entry in earlier] == [3, 2, 1]
    assert api.get(base + "/history/999", headers=auth).status_code == 422
    assert api.delete(url + "/members/" + capture["account_id"], headers=OWNER).status_code == 200
    assert api.get(base + "/history/4", headers=auth).status_code == 403


def test_two_accounts_contribute_distinct_originals_and_revisit_each_story(api, keys):
    url, _, alex, first, base = setup_memory(api, keys)
    sam = signed(keys, "sam")
    join(api, url, sam)
    image = BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    second = api.post(
        url + "/captures",
        content=image.getvalue(),
        headers={
            **sam,
            "Content-Type": "image/png",
            "X-Sweep-Capture": json.dumps(
                {
                    "contributor_id": "client-supplied",
                    "name": "Sam",
                    "kind": "photo",
                    "source": "import",
                }
            ),
        },
    )
    assert second.status_code == 201, second.text
    second = second.json()
    second_base = url + f"/captures/{second['id']}/memory"
    for auth, endpoint, story in (
        (alex, base, "Alex planted seeds"),
        (sam, second_base, "Sam watered the garden"),
    ):
        assert (
            api.post(
                endpoint,
                headers=auth,
                json={
                    "revision": 0,
                    "notes": {"description": story, "occurred_at": "2024-05-18T18:30:00-05:00"},
                },
            ).status_code
            == 200
        )
        assert api.post(endpoint + "/review", headers=auth, json={"revision": 1}).status_code == 200
    assert api.get(base, headers=sam).json()["notes"]["description"] == "Alex planted seeds"
    assert (
        api.get(second_base, headers=alex).json()["notes"]["description"]
        == "Sam watered the garden"
    )
    for auth, own in ((alex, first), (sam, second)):
        timeline = api.get(url + "/timeline", headers=auth).json()["entries"]
        assert len(timeline) == 2
        assert all(entry["time"]["start_date"] == "2024-05-18" for entry in timeline)
        assert {entry["capture"]["id"] for entry in timeline if entry["can_edit"]} == {own["id"]}
    assert first["account_id"] != second["account_id"]
    assert (
        api.get(second_base.removesuffix("/memory") + "/media", headers=alex).content
        == image.getvalue()
    )


def test_legacy_original_cannot_be_claimed_by_account_metadata(api, keys):
    url, created = create_space(api)
    legacy = {"Authorization": "Bearer " + created["contributor_token"]}
    capture = upload(api, url, legacy).json()
    auth = signed(keys)
    join(api, url, auth)
    base = url + f"/captures/{capture['id']}/memory"
    assert not api.get(base, headers=auth).json()["can_edit"]
    assert api.post(base, headers=auth, json={"revision": 0, "notes": {}}).status_code == 403
    assert api.post(base, headers=legacy, json={"revision": 0, "notes": {}}).status_code == 401
    assert api.get(base + "/history", headers=legacy).status_code == 200
    assert (
        api.post(
            base, headers=OWNER, json={"revision": 0, "notes": {}, "actor": "spoof"}
        ).status_code
        == 422
    )
