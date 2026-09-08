from __future__ import annotations

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
