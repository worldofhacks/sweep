from __future__ import annotations

import hashlib
import json

import pytest

from tools.prepare_real_navigation_package import build_package


def _source() -> dict[str, object]:
    identifiers = [*range(29), *range(30, 54)]
    return {
        "kind": "complete_private_provisional_53_tag_map",
        "status": "private_provisional_offline_map_not_for_registry_acceptance",
        "coordinate_frame": {"id": "unit11_atrium_38_to_39_v1"},
        "coverage": {"actual_tag_ids": identifiers, "intentionally_absent_tag_ids": [29]},
        "tags": [
            {"id": tag_id, "center_m": [float(tag_id), float(tag_id % 5), 0.0]}
            for tag_id in identifiers
        ],
    }


def test_stages_both_real_named_formation_areas_without_enabling_flight(tmp_path) -> None:
    source = tmp_path / "final-53-tag-map.json"
    source.write_text(json.dumps(_source()))

    package = build_package(source)

    assert package["activation"] == "blocked"
    assert package["source"]["intentionallyAbsentTagIds"] == [29]
    assert [area["zoneId"] for area in package["formationAreaDrafts"]] == [
        "atrium-front",
        "carpet",
    ]
    assert package["formationBindingContract"]["intentArgs"] == ["name", "zone_id"]
    assert package["formationBindingContract"]["bindings"] == [
        {"shape": "line", "zoneId": "atrium-front"},
        {"shape": "column", "zoneId": "atrium-front"},
        {"shape": "line", "zoneId": "carpet"},
        {"shape": "column", "zoneId": "carpet"},
    ]
    for area in package["formationAreaDrafts"]:
        assert area["flightAuthorization"] == {
            "ownerApproved": False,
            "enabled": False,
            "verticalClearanceM": None,
            "horizontalClearanceM": None,
            "speedMps": None,
            "altitudeOffsetsM": None,
        }
        assert len(area["tagGridOutline"]) == 5


@pytest.mark.parametrize("field, value", [("status", "accepted"), ("kind", "other")])
def test_rejects_source_that_could_hide_its_provisional_status(
    tmp_path, field: str, value: str
) -> None:
    source = tmp_path / "final-53-tag-map.json"
    payload = _source()
    payload[field] = value
    source.write_text(json.dumps(payload))

    with pytest.raises(ValueError):
        build_package(source)


def test_rejects_missing_or_extra_tag_coverage(tmp_path) -> None:
    source = tmp_path / "final-53-tag-map.json"
    payload = _source()
    payload["coverage"]["actual_tag_ids"] = list(range(54))
    source.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="53 retained"):
        build_package(source)


def _approval(source) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "scope": "tag_map_baseline",
        "ownerApproved": True,
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "coordinateFrame": "unit11_atrium_38_to_39_v1",
        "recordedAt": "2026-09-08",
    }


def test_map_approval_accepts_geometry_baseline_without_enabling_unmeasured_flight(tmp_path):
    source = tmp_path / "map.json"
    source.write_text(json.dumps(_source()))
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps(_approval(source)))

    package = build_package(source, map_approval=approval)

    assert package["mapApproval"]["status"] == "accepted"
    checks = {check["id"]: check["status"] for check in package["activationChecks"]}
    assert checks["accepted-tag-map"] == "passed"
    assert checks["measured-route-geometry"] == "blocked"
    assert package["activation"] == "blocked"
    assert not any(
        area["flightAuthorization"]["enabled"] for area in package["formationAreaDrafts"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("sourceSha256", "0" * 64),
        ("coordinateFrame", "different-frame"),
        ("scope", "flight_authorization"),
        ("ownerApproved", False),
    ],
)
def test_approval_cannot_be_reused_for_different_map_or_authority(tmp_path, field, value):
    source = tmp_path / "map.json"
    source.write_text(json.dumps(_source()))
    approval = tmp_path / "approval.json"
    record = _approval(source)
    record[field] = value
    approval.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="exact source"):
        build_package(source, map_approval=approval)


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_tag_coordinates_cannot_enter_staged_geometry(tmp_path, coordinate):
    payload = _source()
    payload["tags"][0]["center_m"][0] = coordinate
    source = tmp_path / "map.json"
    source.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="invalid center"):
        build_package(source)


def test_duplicate_tag_cannot_silently_replace_approved_geometry(tmp_path):
    payload = _source()
    payload["tags"].append({"id": 0, "center_m": [50, 50, 0]})
    source = tmp_path / "map.json"
    source.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="duplicate tag"):
        build_package(source)


def _tag_availability(source, unavailable_tags) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "unavailableTags": unavailable_tags,
    }


def test_stages_unavailable_tags_as_localization_exclusions_only(tmp_path) -> None:
    source = tmp_path / "map.json"
    source.write_text(json.dumps(_source()))
    availability = tmp_path / "tag-availability.json"
    availability.write_text(
        json.dumps(
            _tag_availability(
                source,
                [
                    {"tagId": 49, "reason": "ripped and unusable"},
                    {"tagId": 35, "reason": "ripped and unusable"},
                ],
            )
        )
    )

    package = build_package(source, tag_availability=availability)

    assert package["tagAvailability"] == {
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "unavailableTags": [
            {"tagId": 49, "reason": "ripped and unusable"},
            {"tagId": 35, "reason": "ripped and unusable"},
        ],
        "localizationCandidateTagIds": [
            tag_id for tag_id in [*range(29), *range(30, 54)] if tag_id not in {35, 49}
        ],
    }
    assert package["activation"] == "blocked"
    assert all(
        not area["flightAuthorization"]["enabled"] for area in package["formationAreaDrafts"]
    )


@pytest.mark.parametrize(
    "unavailable_tags,error",
    [
        ([{"tagId": 35, "reason": "ripped"}, {"tagId": 35, "reason": "unusable"}], "duplicate"),
        ([{"tagId": 29, "reason": "missing"}], "retained tag ID"),
        ([{"tagId": "35", "reason": "ripped"}], "retained tag ID"),
        ([{"tagId": 35, "reason": "  "}], "include a reason"),
    ],
)
def test_rejects_duplicate_or_unretained_tag_availability_entries(
    tmp_path, unavailable_tags, error
) -> None:
    source = tmp_path / "map.json"
    source.write_text(json.dumps(_source()))
    availability = tmp_path / "tag-availability.json"
    availability.write_text(json.dumps(_tag_availability(source, unavailable_tags)))

    with pytest.raises(ValueError, match=error):
        build_package(source, tag_availability=availability)


def test_rejects_tag_availability_for_a_different_source(tmp_path) -> None:
    source = tmp_path / "map.json"
    source.write_text(json.dumps(_source()))
    availability = tmp_path / "tag-availability.json"
    record = _tag_availability(source, [{"tagId": 35, "reason": "ripped"}])
    record["sourceSha256"] = "0" * 64
    availability.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="exact source"):
        build_package(source, tag_availability=availability)
