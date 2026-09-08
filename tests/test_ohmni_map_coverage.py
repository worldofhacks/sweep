from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from tools.ohmni_map_coverage import report

sys.path.insert(0, str(Path(__file__).parent))
local_fixture = importlib.import_module("test_ohmni_local_map_candidate")


def test_reports_covered_and_revisit_tags_from_accepted_archives(tmp_path: Path) -> None:
    archive, *_ = local_fixture._write_candidate_inputs(tmp_path)

    result = report([archive], [7, 8], 100_000)

    assert result["revisit_tag_ids"] == [8]
    tag = result["tags"][0]
    assert tag["tag_id"] == 7
    assert tag["status"] == "coverage_candidate"
    assert tag["preliminary_capture_associated_observations"] == 2
    assert result["chunks"][0]["stop_reason"] == "input_exhausted"


def test_refuses_chunks_that_do_not_prove_a_common_epoch(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first, *_ = local_fixture._write_candidate_inputs(first_root)
    second, *_ = local_fixture._write_candidate_inputs(second_root)
    observations = (
        (second / "observations.jsonl")
        .read_bytes()
        .replace(b'"connection_epoch":4', b'"connection_epoch":5')
    )
    (second / "observations.jsonl").write_bytes(observations)
    manifest = json.loads((second / "manifest.json").read_text())
    manifest["scope"]["connection_epoch"] = 5
    manifest["observations"]["sha256"] = __import__("hashlib").sha256(observations).hexdigest()
    (second / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="exact session, epoch"):
        report([first, second], [7], 100_000)


def test_reads_expected_tag_ids_from_the_office_inventory(tmp_path: Path) -> None:
    inventory = tmp_path / "office-tags.json"
    inventory.write_text(json.dumps({"expected_tag_ids": [7, 8]}))

    assert __import__("tools.ohmni_map_coverage", fromlist=["_expected"])._expected(inventory) == [
        7,
        8,
    ]
