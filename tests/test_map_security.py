import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from tools import map_validate


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "bundle"
    shutil.copytree(Path(__file__).parent / "fixtures" / "mapping", path)
    return path


def _registry(bundle):
    manifest = json.loads((bundle / "manifest.yaml").read_text())
    return {manifest["bundle_version"]: manifest["content_sha256"]}


def _set_source(bundle, name):
    path = bundle / "manifest.yaml"
    manifest = json.loads(path.read_text())
    manifest["sources"][0]["path"] = name
    manifest["sources"][0]["sha256"] = hashlib.sha256((bundle / name).read_bytes()).hexdigest()
    manifest["content_sha256"] = map_validate.content_hash(manifest)
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("name", ["manifest.yaml", "tags.yaml", "zones.yaml", "obstacles.yaml"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_external_document_alias_is_rejected_despite_matching_hashes(bundle, tmp_path, name, alias):
    registry = _registry(bundle)
    external = tmp_path / name
    (bundle / name).rename(external)
    if alias == "symlink":
        (bundle / name).symlink_to(external)
    else:
        os.link(external, bundle / name)
    with pytest.raises(ValueError):
        map_validate.validate_bundle(bundle, registry)


@pytest.mark.parametrize("name", ["./tags.yaml", "./zones.yaml", "./obstacles.yaml"])
def test_source_cannot_use_normalized_reserved_document_name(bundle, name):
    _set_source(bundle, name)
    with pytest.raises(ValueError):
        map_validate.validate_bundle(bundle, _registry(bundle))


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_source_cannot_hide_reserved_document_behind_alias(bundle, alias):
    if alias == "symlink":
        (bundle / "scan-alias.ply").symlink_to(bundle / "tags.yaml")
    else:
        os.link(bundle / "tags.yaml", bundle / "scan-alias.ply")
    _set_source(bundle, "scan-alias.ply")
    with pytest.raises(ValueError):
        map_validate.validate_bundle(bundle, _registry(bundle))


def test_source_cannot_traverse_external_symlink_directory(bundle, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "scan.ply").write_bytes((bundle / "scan.ply").read_bytes())
    (bundle / "sources").symlink_to(external, target_is_directory=True)
    _set_source(bundle, "sources/scan.ply")
    with pytest.raises(ValueError):
        map_validate.validate_bundle(bundle, _registry(bundle))


def test_hash_then_parse_swap_cannot_accept_unhashed_tag_sizes(bundle, monkeypatch):
    registry = _registry(bundle)
    path = bundle / "tags.yaml"
    original = json.loads(path.read_text())
    replacement = json.loads(path.read_text())
    replacement["tags"][1]["size"] = 0.42
    read_bytes = Path.read_bytes
    swapped = False

    def swap_after_hash_read(self):
        nonlocal swapped
        payload = read_bytes(self)
        if self == path and not swapped:
            swapped = True
            path.write_text(json.dumps(replacement))
        return payload

    monkeypatch.setattr(Path, "read_bytes", swap_after_hash_read)
    try:
        validated = map_validate.validate_bundle(bundle, registry)
    except ValueError:
        return
    observed = (
        validated.document("tags.yaml")
        if hasattr(validated, "document")
        else json.loads(path.read_text())
    )
    assert observed == original


def test_validated_document_preserves_snapshot_after_disk_changes(bundle, monkeypatch):
    registry = _registry(bundle)
    path = bundle / "tags.yaml"
    original = json.loads(path.read_text())
    replacement = json.loads(path.read_text())
    replacement["tags"][1]["size"] = 0.42
    read_snapshot = map_validate.read_bundle_bytes

    def swap_after_snapshot(root, name, **kwargs):
        payload = read_snapshot(root, name, **kwargs)
        if name == "tags.yaml":
            path.write_text(json.dumps(replacement))
        return payload

    monkeypatch.setattr(map_validate, "read_bundle_bytes", swap_after_snapshot)
    validated = map_validate.validate_bundle(bundle, registry)
    assert json.loads(path.read_text()) == replacement
    assert validated.document("tags.yaml") == original
    copy = validated.document("tags.yaml")
    copy["tags"][1]["size"] = 0.99
    assert validated.document("tags.yaml") == original
