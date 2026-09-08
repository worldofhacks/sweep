import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from relay.map_authoring import MapAuthoringError, MapAuthoringStore
from tests.world_bundle_fixtures import fixture_world_draft


@pytest.fixture
def store(tmp_path):
    return MapAuthoringStore(tmp_path / "maps.sqlite3", clock_ms=lambda: 10_000)


def approved(store, draft=None, *, session="session"):
    reference = store.save(session, draft or fixture_world_draft(), None, "console:operator")
    validation = store.validate(session, reference, "console:operator")
    approval = store.approve(session, reference, validation["validationId"], "console:reviewer")
    return reference, validation, approval


def test_empty_store_never_manufactures_fixture_or_approval(store):
    assert store.list("session") == []
    with pytest.raises(MapAuthoringError, match="no current approved"):
        store.approved_bundle("session")


def test_fixture_create_validate_approve_reopen_compare_complete_workflow(store):
    draft = fixture_world_draft()
    first, validation, approval = approved(store, draft)
    assert validation["valid"] and validation["issues"] == []
    assert approval["approvedBy"] == "console:reviewer"
    assert approval["reference"] == first
    reopened = MapAuthoringStore(store.database_path, clock_ms=lambda: 10_001)
    assert reopened.load("session", first) == {"reference": first, "draft": draft}
    assert reopened.approved_bundle("session")["approval"] == approval
    assert reopened.list("session")[0]["contentHash"] == first["contentHash"]
    revised = copy.deepcopy(draft)
    revised["metadata"]["mapVersion"] = "fixture-v2"
    revised["features"][1]["name"] = "Reception"
    second = reopened.save("session", revised, first, "console:editor")
    comparison = reopened.compare("session", first, second)
    assert {c["path"] for c in comparison["changes"]} == {"metadata.mapVersion", "features.1.name"}
    assert reopened.load("session", first)["draft"] == draft
    with pytest.raises(MapAuthoringError, match="no current approved"):
        reopened.approved_bundle("session")
    checked = reopened.validate("session", second, "console:editor")
    reopened.approve("session", second, checked["validationId"], "console:reviewer")
    assert reopened.approved_bundle("session")["reference"] == second
    assert [a["action"] for a in reopened.audit_records("session")] == [
        "map_saved",
        "map_validated",
        "map_approved",
        "map_saved",
        "map_validated",
        "map_approved",
    ]


def test_save_runs_validation_and_preserves_incomplete_editable_draft(store):
    draft = fixture_world_draft()
    draft["metadata"].pop("registration")
    reference = store.save("session", draft, None, "console:editor")
    assert store.load("session", reference)["draft"] == draft
    saved = store.audit_records("session")[0]
    validation = saved["payload"]["validation"]
    assert validation["reference"] == reference and not validation["valid"]
    with pytest.raises(MapAuthoringError) as caught:
        store.approve("session", reference, validation["validationId"], "console:reviewer")
    assert caught.value.code == "validation_failed"


def test_receipts_cannot_cross_revisions_sessions_or_content_hashes(store):
    first, validation, _ = approved(store)
    with pytest.raises(MapAuthoringError) as caught:
        store.load("other-session", first)
    assert caught.value.code == "revision_not_found"
    forged = {**first, "contentHash": "0" * 64}
    with pytest.raises(MapAuthoringError) as caught:
        store.load("session", forged)
    assert caught.value.code == "revision_mismatch"
    draft = fixture_world_draft()
    draft["metadata"]["mapVersion"] = "fixture-v2"
    second = store.save("session", draft, first, "console:editor")
    with pytest.raises(MapAuthoringError) as caught:
        store.approve("session", second, validation["validationId"], "console:reviewer")
    assert caught.value.code == "validation_mismatch"
    with pytest.raises(MapAuthoringError) as caught:
        store.approve("session", first, validation["validationId"], "console:reviewer")
    assert caught.value.code == "revision_conflict"


def test_changed_approved_map_version_requires_new_version(store):
    first, _, _ = approved(store)
    draft = fixture_world_draft()
    draft["features"][1]["name"] = "Changed room"
    second = store.save("session", draft, first, "console:editor")
    validation = store.validate("session", second, "console:editor")
    with pytest.raises(MapAuthoringError) as caught:
        store.approve("session", second, validation["validationId"], "console:reviewer")
    assert caught.value.code == "map_version_reused"


def test_stale_expected_revision_is_transactional_across_connections(store):
    first = store.save("session", fixture_world_draft(), None, "console:editor")

    def write():
        try:
            return MapAuthoringStore(store.database_path, clock_ms=lambda: 10_000).save(
                "session", fixture_world_draft(), first, "console:editor"
            )
        except MapAuthoringError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: write(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "revision_conflict" in results
    assert len(store.list("session")) == 2


def test_authored_approval_claims_do_not_create_authority(store):
    draft = fixture_world_draft()
    draft["approval"] = {"approvedBy": "owner", "valid": True}
    reference = store.save("session", draft, None, "console:editor")
    validation = store.validate("session", reference, "console:editor")
    assert not validation["valid"]
    with pytest.raises(MapAuthoringError):
        store.approve("session", reference, validation["validationId"], "console:editor")


def test_approval_is_idempotent_and_never_relabels_authenticated_actor(store):
    reference, validation, approval = approved(store)
    again = store.approve("session", reference, validation["validationId"], "console:another")
    assert again == approval
    assert len([a for a in store.audit_records("session") if a["action"] == "map_approved"]) == 1
    revalidated = store.validate("session", reference, "console:another")
    assert revalidated["validationId"] == validation["validationId"]
    assert (
        store.approve("session", reference, revalidated["validationId"], "console:another")
        == approval
    )


def test_creation_time_uses_service_clock_and_legal_session_lengths(store):
    draft = fixture_world_draft()
    draft["metadata"]["createdAt"] = 10_001
    session = "é" * 512
    reference = store.save(session, draft, None, "console:editor")
    validation = store.validate(session, reference, "console:editor")
    assert not validation["valid"]
    assert any("future" in i["message"] for i in validation["issues"])
    with pytest.raises(MapAuthoringError):
        store.list(session + "é")


def test_multiple_approved_maps_require_explicit_revision(store):
    first, _, _ = approved(store)
    draft = fixture_world_draft()
    draft["metadata"]["mapVersion"] = "another-map"
    second, _, _ = approved(store, draft)
    with pytest.raises(MapAuthoringError) as caught:
        store.approved_bundle("session")
    assert caught.value.code == "approval_ambiguous"
    assert store.approved_bundle("session", first)["reference"] == first
    assert store.approved_bundle("session", second)["reference"] == second


@pytest.mark.parametrize(
    "table", ["map_revisions", "map_validations", "map_approvals", "map_audit"]
)
def test_database_refuses_mutation_of_immutable_evidence(store, table):
    approved(store)
    with sqlite3.connect(store.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"DELETE FROM {table}")


def test_missing_validator_receipt_never_reuses_approval(store):
    reference, _, _ = approved(store)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TRIGGER map_validations_immutable_delete")
        connection.execute("DELETE FROM map_validations")
    with pytest.raises(MapAuthoringError) as caught:
        store.approved_bundle("session", reference)
    assert caught.value.code == "integrity_error"


def test_quota_refusal_rolls_back_without_advancing_head(store, monkeypatch):
    reference = store.save("session", fixture_world_draft(), None, "console:editor")
    monkeypatch.setattr("relay.map_authoring.MAX_SESSION_REVISIONS", 1)
    with pytest.raises(MapAuthoringError) as caught:
        store.save("session", fixture_world_draft(), reference, "console:editor")
    assert caught.value.code == "storage_quota"
    assert len(store.list("session")) == len(store.audit_records("session")) == 1
    assert store.load("session", reference)["reference"] == reference


def test_list_is_bounded_to_latest_256_revisions(store):
    draft = {
        "format": "sweep-map-draft-v1",
        "metadata": {},
        "image": None,
        "features": [],
        "tags": [],
    }
    reference = None
    for _ in range(257):
        reference = store.save("session", draft, reference, "console:editor")
    listed = store.list("session")
    assert len(listed) == 256
    assert listed[0]["revision"] == "257" and listed[-1]["revision"] == "2"


def test_comparison_preserves_json_types_and_absent_fields(store):
    draft = fixture_world_draft()
    first = store.save("session", draft, None, "console:editor")
    draft["metadata"]["createdAt"] = 1000.0
    draft["unknown"] = None
    second = store.save("session", draft, first, "console:editor")
    changes = {
        change["path"]: change for change in store.compare("session", first, second)["changes"]
    }
    assert changes["metadata.createdAt"]["before"] == "1000"
    assert changes["metadata.createdAt"]["after"] == "1000.0"
    assert changes["unknown"]["before"] == "<absent>"
    assert changes["unknown"]["after"] == "null"
