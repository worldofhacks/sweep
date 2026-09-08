from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

from relay.observations import Observation, decode_observation
from tools.ohmni_multi_archive_tag_candidate import MAX_MULTI_ARCHIVE_OBSERVATIONS, build

sys.path.insert(0, str(Path(__file__).parent))
local_fixture = importlib.import_module("test_ohmni_local_map_candidate")


def _rewrite_archive(path: Path, events: list[Observation], *, epoch: int | None = None) -> None:
    if epoch is not None:
        events = [Observation(replace(event.submission, connection_epoch=epoch), event.t_ingest) for event in events]
    payload = b"".join(event.encode() + b"\n" for event in events)
    manifest = json.loads((path / "manifest.json").read_text())
    if epoch is not None:
        manifest["scope"]["connection_epoch"] = epoch
    kinds = {kind: sum(event.submission.payload["kind"] == kind for event in events) for kind in ("camera_frame", "tag_observation", "pose", "range_scan")}
    manifest["observations"].update({"count": len(events), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "kinds": kinds})
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
    return [decode_observation(line) for line in (path / "observations.jsonl").read_bytes().splitlines()]


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


def test_fuses_more_than_the_single_archive_limit_from_validated_chunks(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    body = [event for event in events if event.submission.payload["kind"] == "pose"][-1]
    padded = events + [Observation(replace(body.submission, event_id=f"body-pad-{index}"), body.t_ingest) for index in range(1_100)]
    first = _copy_archive(archive, tmp_path / "first", padded[:600])
    second = _copy_archive(archive, tmp_path / "second", padded[600:])

    result = build([first, second], request, evidence, tmp_path / "output", 100)

    assert len(result["candidates"]) == 1
    assert result["aggregate_observations"]["path"] == "inputs/combined-observations.jsonl"


def test_refuses_aggregate_event_cap_plus_one_before_publishing(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    body = [event for event in events if event.submission.payload["kind"] == "pose"][-1]
    expanded = events + [Observation(replace(body.submission, event_id=f"body-pad-{index}"), body.t_ingest) for index in range(MAX_MULTI_ARCHIVE_OBSERVATIONS)]
    chunks = [
        _copy_archive(archive, tmp_path / f"chunk-{index}", expanded[index * 1024 : (index + 1) * 1024])
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
            payload = {**event.submission.payload, "pose": {**event.submission.payload["pose"], "x_m": 5.0}}
            altered.append(Observation(replace(event.submission, payload=payload), event.t_ingest))
        else:
            altered.append(event)
    second = _copy_archive(archive, tmp_path / "second", altered)

    with pytest.raises(ValueError, match="continuity translation"):
        build([first, second], request, evidence, tmp_path / "output", 100)


def test_refuses_disagreeing_shared_tag_without_publishing(tmp_path: Path) -> None:
    archive, _, request, evidence, _ = local_fixture._write_candidate_inputs(tmp_path)
    events = _events(archive)
    first = _copy_archive(archive, tmp_path / "first", events[:3])
    altered = []
    for event in events[3:]:
        if event.submission.payload["kind"] == "tag_observation":
            payload = {**event.submission.payload, "tag_pose": {**event.submission.payload["tag_pose"], "x_m": 4.0}}
            altered.append(Observation(replace(event.submission, payload=payload), event.t_ingest))
        else:
            altered.append(event)
    second = _copy_archive(archive, tmp_path / "second", altered)

    with pytest.raises(ValueError, match="shared tag observations do not agree"):
        build([first, second], request, evidence, tmp_path / "output", 100)
