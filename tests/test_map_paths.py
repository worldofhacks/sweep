import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from tools import map_common
from tools.map_validate import content_hash, validate_bundle


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "bundle"
    shutil.copytree(Path(__file__).parent / "fixtures" / "mapping", path)
    return path


@pytest.mark.parametrize("name", ["manifest.yaml", "tags.yaml", "zones.yaml", "obstacles.yaml"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_bundle_documents_cannot_alias_external_files(bundle, tmp_path, name, alias):
    original = bundle / name
    external = tmp_path / name
    original.rename(external)
    if alias == "symlink":
        original.symlink_to(external)
    else:
        os.link(external, original)
    with pytest.raises(ValueError):
        validate_bundle(bundle, {"synthetic-three-tags-v1"})
    assert external.read_bytes() == original.read_bytes()


@pytest.mark.parametrize("name", ["./tags.yaml", "././zones.yaml", "./manifest.yaml"])
def test_normalized_reserved_source_names_are_rejected(bundle, name):
    with pytest.raises(ValueError, match="bundle document"):
        map_common.source_path(bundle, name)


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_source_cannot_alias_reserved_document_under_another_name(bundle, alias):
    source = bundle / "alias.ply"
    if alias == "symlink":
        source.symlink_to(bundle / "tags.yaml")
    else:
        os.link(bundle / "tags.yaml", source)
    manifest_path = bundle / "manifest.yaml"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["sources"][0]["path"] = "alias.ply"
    manifest["sources"][0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest["content_sha256"] = content_hash(manifest)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        validate_bundle(bundle, {"synthetic-three-tags-v1"})


@pytest.mark.parametrize("name", ["../outside", "/etc/passwd", ".", "missing.ply"])
def test_source_requires_a_confined_existing_regular_file(bundle, name):
    with pytest.raises((ValueError, OSError)):
        map_common.source_path(bundle, name)


def test_sources_cannot_traverse_symlinked_directories(bundle):
    directory = bundle / "sources"
    directory.mkdir()
    (directory / "raw.ply").write_bytes(b"source")
    (bundle / "shortcut").symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        map_common.source_path(bundle, "shortcut/raw.ply")
    assert map_common.read_bundle_bytes(bundle, "sources/raw.ply", source=True) == b"source"


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', '{"x":NaN}', '{"x":[1e400]}', "[]"])
def test_snapshot_parser_preserves_duplicate_and_finite_checks(payload):
    with pytest.raises(ValueError):
        map_common.parse_document(payload)


def test_snapshot_parser_reads_the_same_bytes_that_are_hashed(bundle):
    payload = map_common.read_bundle_bytes(bundle, "tags.yaml")
    assert map_common.parse_document(payload) == map_common.read_document(bundle / "tags.yaml")


def test_snapshot_open_rejects_symlink_swapped_after_preflight(bundle, tmp_path, monkeypatch):
    external = tmp_path / "outside.yaml"
    external.write_text('{"outside":true}')
    original = map_common.bundle_document_path

    def swap_after_check(root, name):
        path = original(root, name)
        path.unlink()
        path.symlink_to(external)
        return path

    monkeypatch.setattr(map_common, "bundle_document_path", swap_after_check)
    with pytest.raises(OSError):
        map_common.read_bundle_bytes(bundle, "tags.yaml")
