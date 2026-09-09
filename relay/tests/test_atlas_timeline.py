import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from relay.atlas import AtlasError, AtlasStore, CaptureMetadata, NewSpace
from relay.atlas_timeline import CaptureDate, SaveCaptureDate, project_time
from relay.tests.test_atlas_accounts import (
    OWNER,
    PNG,
    SPACE,
    create_space,
    join,
    signed,
    upload,
)
from relay.tests.test_atlas_accounts import (
    api as api,
)
from relay.tests.test_atlas_accounts import (
    keys as keys,
)


@pytest.mark.parametrize(
    "value",
    [
        {"precision": "day", "value": "2024-02-30"},
        {"precision": "instant", "value": "2024-11-03T01:30:00"},
        {"precision": "range", "value": "2024-01-02", "end": "2024-01-01"},
        {"precision": "year", "value": "24"},
        {"precision": "month", "value": "2024-13"},
        {"precision": "day", "value": "9999-01-01"},
        {"precision": "unknown", "value": "2024-01-01"},
    ],
)
def test_date_contract_refuses_invented_precision(value):
    with pytest.raises(ValidationError):
        CaptureDate(**value)


def test_unknown_zone_exif_and_clock_skew_are_not_hidden():
    capture = {"captured_at": None}
    time, evidence, warnings = project_time(
        capture, {"inspection": {"local_timestamp": "2024-11-03T01:30:00"}}
    )
    assert time["precision"] == "day"
    assert time["source"] == "metadata_local"
    assert time["value"] == "2024-11-03"
    assert evidence[0]["value"] == "2024-11-03T01:30:00"
    assert "time zone unknown" in time["note"]
    assert not warnings
    future = int((datetime.now(UTC) + timedelta(seconds=45)).timestamp() * 1000)
    time, _, warnings = project_time({"captured_at": future}, {})
    assert time["precision"] == "instant"
    assert warnings


def test_projection_uses_grounded_analysis_metadata_not_generated_prose():
    time, evidence, _ = project_time(
        {"captured_at": None},
        {
            "analysis": {
                "inspection": {"timestamp": "2020-06-01T14:30:00-05:00"},
                "suggestion": {"summary": "An imagined winter day in 1900"},
            },
        },
    )
    assert time["value"] == "2020-06-01T14:30:00-05:00"
    assert time["source"] == "metadata"
    assert len(evidence) == 1
    time, evidence, _ = project_time(
        {"captured_at": 1_600_000_000_000},
        {
            "notes": {"occurred_at": "2019-06-01T12:00:00+01:00"},
        },
    )
    assert time["source"] == "memory_note"
    assert time["start_date"] == "2019-06-01"
    assert len(evidence) == 2


def test_account_owner_can_correct_guest_dates_but_unrelated_owners_cannot(api, keys):
    owner, guest = signed(keys), signed(keys, "guest")
    response = api.post(
        "/api/atlas/account/spaces",
        headers=owner,
        json={
            "draft_id": "9a3a601a-5265-41f6-b1cd-3e1f3cf7354d",
            "space": SPACE,
        },
    )
    grant = response.json()
    url = f"/api/sessions/{grant['session']}/atlas/spaces/{grant['space_id']}"
    invitation = api.post(url + "/account-invitations", headers=owner, json={}).json()
    api.post(
        "/api/atlas/account/invitations/accept", headers=guest, json={"token": invitation["token"]}
    )
    capture = upload(api, url, guest).json()
    endpoint = url + f"/captures/{capture['id']}/date"
    payload = {"revision": 0, "assertion": {"precision": "year", "value": "2020"}}
    assert api.post(endpoint, headers=owner, json=payload).status_code == 200
    assert api.post(endpoint, headers=signed(keys, "unrelated"), json=payload).status_code == 403
    assert api.get(url + "/timeline", headers=owner).json()["entries"][0]["can_edit"] is True


def test_calendar_precision_dst_and_late_upload_order(tmp_path):
    store = AtlasStore(tmp_path)
    space = store.create("timeline", NewSpace(**SPACE))["space"]["id"]
    captures = []
    for number in range(5):
        staged = tmp_path / f"capture-{number}.png"
        staged.write_bytes(PNG + str(number).encode())
        captures.append(
            store.add_capture(
                space,
                CaptureMetadata(
                    contributor_id="test-person",
                    name="Maya",
                    kind="photo",
                    source="import",
                    captured_at=None,
                    position=None,
                ),
                staged,
                "image/png",
            )
        )
    assertions = [
        {"precision": "instant", "value": "2024-11-03T01:30:00-05:00"},
        {"precision": "instant", "value": "2024-11-03T01:30:00-06:00"},
        {"precision": "year", "value": "2020"},
        {"precision": "month", "value": "2024-02"},
    ]
    for capture, value in zip(captures, assertions, strict=False):
        store.timeline.save(
            space,
            capture["id"],
            SaveCaptureDate(revision=0, assertion=CaptureDate(**value)),
            operator=True,
        )
    result = store.timeline.entries(space, operator=True)["entries"]
    assert [e["capture"]["id"] for e in result] == [captures[i]["id"] for i in (1, 0, 3, 2, 4)]
    assert result[2]["time"]["end_date"] == "2024-02-29"
    assert result[0]["time"]["utc_offset"] == "-06:00"
    assert result[1]["time"]["utc_offset"] == "-05:00"
    assert result[2]["time"]["utc_offset"] is None
    assert result[3]["time"]["start_date"] == "2020-01-01"
    assert result[3]["time"]["end_date"] == "2020-12-31"
    assert result[-1]["time"]["precision"] == "unknown"
    assert all(e["capture"]["position"] is None for e in result)
    # Ties are stable regardless of SQLite row order and UI paging boundaries.
    for capture in captures[:2]:
        store.timeline.save(
            space,
            capture["id"],
            SaveCaptureDate(revision=1, assertion=CaptureDate(precision="day", value="2024-11-03")),
            operator=True,
        )
    tied = store.timeline.entries(space)["entries"][:2]
    assert [e["capture"]["id"] for e in tied] == sorted(
        [c["id"] for c in captures[:2]], reverse=True
    )
    store.close()


def test_date_history_preserves_originals_and_membership_boundaries(api, keys):
    url, created = create_space(api)
    author, other, viewer = signed(keys), signed(keys, "other"), signed(keys, "viewer")
    join(api, url, author)
    join(api, url, other)
    join(api, url, viewer, "viewer")
    capture = upload(api, url, author).json()
    endpoint = url + f"/captures/{capture['id']}/date"
    timeline = url + "/timeline"
    assert api.get(timeline).status_code == 401
    assert api.get(timeline, headers=signed(keys, "outsider")).status_code == 403
    assert api.get(timeline, headers=viewer).json()["entries"][0]["can_edit"] is False
    payload = {
        "revision": 0,
        "assertion": {
            "precision": "range",
            "value": "2020-06-01",
            "end": "2020-06-07",
            "note": "Our first week here.",
        },
    }
    for denied in (viewer, other, {"Authorization": "Bearer " + created["contributor_token"]}):
        assert api.post(endpoint, headers=denied, json=payload).status_code == 403
    saved = api.post(endpoint, headers=author, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.headers["Cache-Control"] == "no-store"
    assert api.post(endpoint, headers=author, json=payload).status_code == 409
    updated = {"revision": 1, "assertion": {"precision": "unknown", "note": "We aren't sure."}}
    assert api.post(endpoint, headers=OWNER, json=updated).status_code == 200
    history = api.get(endpoint, headers=viewer).json()["history"]
    assert [entry["revision"] for entry in history] == [2, 1]
    assert history[1]["assertion"] == {**payload["assertion"]}
    assert api.get(url, headers=author).json()["captures"][0] == capture
    assert upload(api, url, author).json()["id"] == capture["id"]
    assert api.get(timeline, headers=author).json()["total"] == 1
    assert api.get(url + f"/captures/{capture['id']}/media", headers=author).content == PNG
    assert api.get(timeline, headers=author).json()["entries"][0]["time"]["precision"] == "unknown"
    account = api.post("/api/atlas/account", headers=author).json()["account"]["id"]
    assert api.delete(url + "/members/" + account, headers=OWNER).status_code == 200
    assert api.get(timeline, headers=author).status_code == 403
    assert api.get(endpoint, headers=author).status_code == 403
    assert api.post(endpoint, headers=author, json={**payload, "revision": 2}).status_code == 403


def test_removed_member_cannot_commit_correction_and_unknown_does_not_erase_source(api, keys):
    url, created = create_space(api)
    auth = signed(keys)
    join(api, url, auth)
    capture = upload(api, url, auth, captured_at=1_600_000_000_000).json()
    store = api.app.state.atlas_store
    account = api.post("/api/atlas/account", headers=auth).json()["account"]["id"]
    request = SaveCaptureDate(revision=0, assertion=CaptureDate(precision="unknown"))
    store.accounts.remove(created["space"]["id"], account)
    with pytest.raises(AtlasError):
        store.timeline.save(created["space"]["id"], capture["id"], request, account)
    saved = store.timeline.save(created["space"]["id"], capture["id"], request, operator=True)
    assert saved["evidence"] == [{"source": "capture", "value": 1_600_000_000_000}]
    assert saved["previous"]["precision"] == "instant"
    raw = store.db.execute("SELECT data FROM captures WHERE id=?", (capture["id"],)).fetchone()[0]
    assert json.loads(raw)["captured_at"] == 1_600_000_000_000
