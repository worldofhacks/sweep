from __future__ import annotations

import hashlib
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from relay.observations import Observation, decode_observation
from tools import ohmni_multi_archive_tag_candidate
from tools.ohmni_multi_archive_tag_candidate import (
    MAX_MULTI_ARCHIVE_OBSERVATIONS,
    build,
    build_map,
)

sys.path.insert(0, str(Path(__file__).parent))
local_fixture = importlib.import_module("test_ohmni_local_map_candidate")


def _rewrite_archive(path: Path, events: list[Observation], *, epoch: int | None = None) -> None:
    if epoch is not None:
        events = [
            Observation(replace(event.submission, connection_epoch=epoch), event.t_ingest)
            for event in events
        ]
    payload = b"".join(event.encode() + b"\n" for event in events)
    manifest = json.loads((path / "manifest.json").read_text())
    if epoch is not None:
        manifest["scope"]["connection_epoch"] = epoch
    kinds = {
        kind: sum(event.submission.payload["kind"] == kind for event in events)
        for kind in ("camera_frame", "tag_observation", "pose", "range_scan")
    }
    manifest["observations"].update(
        {
            "count": len(events),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "kinds": kinds,
        }
    )
    manifest["limits"]["max_records"] = max(manifest["limits"]["max_records"], len(events))
    manifest["limits"]["max_bytes"] = max(manifest["limits"]["max_bytes"], len(payload))
    (path / "observations.jsonl").write_bytes(payload)
    (path / "manifest.json").write_text(json.dumps(manifest))


def _copy_archive(source: Path, destination: Path, events: list[Observation]) -> Path:
    destination.mkdir()
    (destination / "manifest.json").write_bytes((source / "manifest.json").read_bytes())
    _rewrite_archive(destination, events)
    return destination


def _events(path: Path) -> list[Observation]:
    return [
        decode_observation(line) for line in (path / "observations.jsonl").read_bytes().splitlines()
    ]


def _bind_request_observations(request: Path, events: list[Observation]) -> None:
    document = json.loads(request.read_text())
    payload = b"".join(event.encode() + b"\n" for event in events)
    document["observations"]["sha256"] = hashlib.sha256(payload).hexdigest()
    request.write_text(json.dumps(document))


def _collection(path: Path, archives: list[Path], handoff: Observation) -> Path:
    payloads = [(archive / "observations.jsonl").read_bytes() for archive in archives]
    raw = b"".join(payloads)
    unique_events = _events(archives[0]) + _events(archives[1])[1:]
    unique = b"".join(event.encode() + b"\n" for event in unique_events)
    captured, received = handoff.submission.t_capture, handoff.submission.t_source_receipt
    assert captured is not None and received is not None
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "ohmni_continuous_mapper_collection",
                "archives": [
                    {
                        "path": archive.name,
                        "manifest_sha256": hashlib.sha256(
                            (archive / "manifest.json").read_bytes()
                        ).hexdigest(),
                        "observations_sha256": hashlib.sha256(payload).hexdigest(),
                    }
                    for archive, payload in zip(archives, payloads, strict=True)
                ],
                "handoffs": [
                    {
                        "from_archive": 0,
                        "to_archive": 1,
                        "source_id": handoff.submission.source_id,
                        "event_id": handoff.submission.event_id,
                        "observation_sha256": hashlib.sha256(handoff.encode()).hexdigest(),
                        "t_capture": {
                            "clock_id": captured.clock_id,
                            "unit": captured.unit,
                            "value": captured.value,
                        },
                        "t_source_receipt": {
                            "clock_id": received.clock_id,
                            "unit": received.unit,
                            "value": received.value,
                        },
                    }
                ],
                "raw_observations": {
                    "count": sum(len(_events(archive)) for archive in archives),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
                "unique_observations": {
                    "count": len(unique_events),
                    "bytes": len(unique),
                    "sha256": hashlib.sha256(unique).hexdigest(),
                },
            }
        )
    )
    return path


def test_single_and_continuous_split_archives_produce_equivalent_candidates(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    output_one = tmp_path / "one"
    one = build([archive], request, evidence, output_one, 100)
    events = _events(archive)
    first = _copy_archive(archive, tmp_path / "first", events[:3])
    second = _copy_archive(archive, tmp_path / "second", events[3:])
    split = build([first, second], request, evidence, tmp_path / "split", 100)

    assert split["candidates"] == one["candidates"]
    assert split["continuity"]["checked_boundaries"] == 1


def test_build_map_records_every_combined_scan_and_pins_its_inputs(tmp_path: Path) -> None:
    archive, config, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    first = _copy_archive(archive, tmp_path / "first", events[:3])
    second = _copy_archive(archive, tmp_path / "second", events[3:])
    output = tmp_path / "map"

    result = build_map([first, second], config, "lidar-measured", request, evidence, output, 100)

    assert result["approval_status"] == "unapproved"
    assert result["candidate_frame"] == "odom"
    assert result["archive_count"] == 2
    assert (output / "occupancy" / "occupancy.png").is_file()
    assert (output / "inputs" / "lidar-config.json").read_bytes() == config.read_bytes()
    assert json.loads((output / "tag_candidates.json").read_text())["candidates"]
    recorded = (output / "recording" / "observations.jsonl").read_bytes()
    assert recorded.count(b'"kind":"range_scan"') == 1
    assert (
        result["files"]["inputs/combined-observations.jsonl"]["sha256"]
        == hashlib.sha256(b"".join(event.encode() + b"\n" for event in events)).hexdigest()
    )


def test_declared_pose_handoff_replaces_the_adjacent_gap_requirement(tmp_path: Path) -> None:
    archive, config, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    handoff = events[1]
    first = _copy_archive(archive, tmp_path / "first", events[:3])
    second = _copy_archive(archive, tmp_path / "second", [handoff, *events[3:]])
    collection = _collection(tmp_path / "collection.json", [first, second], handoff)
    _bind_request_observations(request, events)

    result = build_map(
        [first, second],
        config,
        "lidar-measured",
        request,
        evidence,
        tmp_path / "map",
        0,
        collection,
    )

    assert result["tag_count"] == 1
    tags = json.loads((tmp_path / "map" / "tag_candidates.json").read_text())
    assert tags["raw_observations"]["count"] == len(events) + 1
    assert tags["unique_observations"]["count"] == len(events)


def test_build_map_refuses_a_lidar_budget_that_clips_combined_scans(tmp_path: Path) -> None:
    archive, config, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    document = json.loads(config.read_text())
    document["recording"]["max_records"] = 1
    config.write_text(json.dumps(document))
    events = _events(archive)
    scan = next(event for event in events if event.submission.payload["kind"] == "range_scan")
    events.append(Observation(replace(scan.submission, event_id="scan-two"), scan.t_ingest))
    _rewrite_archive(archive, events)
    _bind_request_observations(request, events)

    with pytest.raises(ValueError, match="record budget"):
        build_map([archive], config, "lidar-measured", request, evidence, tmp_path / "map", 100)


def test_fuses_more_than_the_single_archive_limit_from_validated_chunks(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    body = [event for event in events if event.submission.payload["kind"] == "pose"][-1]
    padded = events + [
        Observation(replace(body.submission, event_id=f"body-pad-{index}"), body.t_ingest)
        for index in range(1_100)
    ]
    first = _copy_archive(archive, tmp_path / "first", padded[:600])
    second = _copy_archive(archive, tmp_path / "second", padded[600:])
    _bind_request_observations(request, padded)

    output = tmp_path / "output"
    result = build([first, second], request, evidence, output, 100)

    assert len(result["candidates"]) == 1
    assert result["aggregate_observations"]["path"] == "inputs/combined-observations.jsonl"
    assert (output / "inputs" / "calibration.json").read_bytes() == (
        evidence / "calibration.json"
    ).read_bytes()
    assert (output / "inputs" / "mount.json").read_bytes() == (evidence / "mount.json").read_bytes()
    assert result["calibration"]["path"] == "inputs/calibration.json"
    assert result["mount"]["path"] == "inputs/mount.json"
    assert result["requested_observations"]["sha256"] == result["aggregate_observations"]["sha256"]


def test_refuses_a_request_that_does_not_pin_the_combined_archives(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    document = json.loads(request.read_text())
    document["observations"]["sha256"] = "0" * 64
    request.write_text(json.dumps(document))
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="observation pin does not match combined archives"):
        build([archive], request, evidence, output, 100)

    assert not output.exists()


def test_refuses_an_unconfined_requested_observation_path(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    document = json.loads(request.read_text())
    document["observations"]["path"] = "../outside.jsonl"
    request.write_text(json.dumps(document))
    output = tmp_path / "output"

    with pytest.raises(
        ValueError, match="fusion observations pin path must stay below evidence root"
    ):
        build([archive], request, evidence, output, 100)

    assert not output.exists()


def test_refuses_aggregate_event_cap_plus_one_before_publishing(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    body = [event for event in events if event.submission.payload["kind"] == "pose"][-1]
    expanded = events + [
        Observation(replace(body.submission, event_id=f"body-pad-{index}"), body.t_ingest)
        for index in range(MAX_MULTI_ARCHIVE_OBSERVATIONS)
    ]
    chunks = [
        _copy_archive(
            archive, tmp_path / f"chunk-{index}", expanded[index * 1024 : (index + 1) * 1024]
        )
        for index in range((len(expanded) + 1023) // 1024)
    ]
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="aggregate fusion limit"):
        build(chunks, request, evidence, output, 100)
    assert not output.exists()


def test_refuses_a_changed_epoch_without_publishing(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    first = _copy_archive(archive, tmp_path / "first", _events(archive))
    second = _copy_archive(archive, tmp_path / "second", _events(archive))
    _rewrite_archive(second, _events(second), epoch=5)

    with pytest.raises(ValueError, match="exact session, epoch"):
        build([first, second], request, evidence, tmp_path / "output", 100)


def test_refuses_discontinuous_pose_without_publishing(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    first = _copy_archive(archive, tmp_path / "first", events[:3])
    altered = []
    for event in events[3:]:
        if event.submission.payload["kind"] == "pose":
            payload = {
                **event.submission.payload,
                "pose": {**event.submission.payload["pose"], "x_m": 5.0},
            }
            altered.append(Observation(replace(event.submission, payload=payload), event.t_ingest))
        else:
            altered.append(event)
    second = _copy_archive(archive, tmp_path / "second", altered)

    with pytest.raises(ValueError, match="continuity translation"):
        build([first, second], request, evidence, tmp_path / "output", 100)


@pytest.mark.parametrize("change", ["clock", "order"])
def test_refuses_ambiguous_archive_pose_time_order(tmp_path: Path, change: str) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    altered = _events(archive)
    pose_indexes = [
        index for index, event in enumerate(altered) if event.submission.payload["kind"] == "pose"
    ]
    if change == "clock":
        index = pose_indexes[-1]
        event = altered[index]
        altered[index] = Observation(
            replace(
                event.submission,
                t_capture=replace(event.submission.t_capture, clock_id="other-clock"),
                t_source_receipt=replace(event.submission.t_source_receipt, clock_id="other-clock"),
            ),
            event.t_ingest,
        )
    else:
        first, last = pose_indexes
        altered[first], altered[last] = altered[last], altered[first]
    _rewrite_archive(archive, altered)
    _bind_request_observations(request, altered)
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="one capture clock|not monotonic"):
        build([archive], request, evidence, output, 100)

    assert not output.exists()


def test_refuses_a_tag_candidate_in_another_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    original = ohmni_multi_archive_tag_candidate.fuse_observations

    def fuse_then_change_frame(*args: object, **kwargs: object) -> dict[str, object]:
        return {**original(*args, **kwargs), "candidate_frame": "other_odom"}

    monkeypatch.setattr(
        ohmni_multi_archive_tag_candidate, "fuse_observations", fuse_then_change_frame
    )
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="do not match archive odometry frame"):
        build([archive], request, evidence, output, 100)

    assert not output.exists()


def test_refuses_an_empty_tag_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    original = ohmni_multi_archive_tag_candidate.fuse_observations

    def fuse_then_remove_candidates(*args: object, **kwargs: object) -> dict[str, object]:
        return {**original(*args, **kwargs), "candidates": []}

    monkeypatch.setattr(
        ohmni_multi_archive_tag_candidate, "fuse_observations", fuse_then_remove_candidates
    )
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="cannot produce qualified local tag candidates"):
        build([archive], request, evidence, output, 100)

    assert not output.exists()


def test_refuses_disagreeing_shared_tag_without_publishing(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    first = _copy_archive(archive, tmp_path / "first", events[:3])
    altered = []
    for event in events[3:]:
        if event.submission.payload["kind"] == "tag_observation":
            payload = {
                **event.submission.payload,
                "tag_pose": {**event.submission.payload["tag_pose"], "x_m": 4.0},
            }
            altered.append(Observation(replace(event.submission, payload=payload), event.t_ingest))
        else:
            altered.append(event)
    second = _copy_archive(archive, tmp_path / "second", altered)
    _bind_request_observations(request, events[:3] + altered)

    with pytest.raises(ValueError, match="shared tag observations do not agree"):
        build([first, second], request, evidence, tmp_path / "output", 100)
