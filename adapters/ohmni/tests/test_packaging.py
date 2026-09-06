from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from adapters.ohmni.tools import build_payload


def archive(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))


def fixture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    module = root / "adapters" / "ohmni" / "tools" / "build_payload.py"
    module.parent.mkdir(parents=True)
    (root / "nodekit").mkdir()
    (root / "nodekit" / "node.py").write_text("# runtime\n")
    (root / "nodekit" / ".env").write_text("secret-fixture")
    (module.parent.parent / "device.py").write_text("# device\n")
    (module.parent.parent / "run.sh").write_text("#!/system/bin/sh\n")
    (module.parent.parent / "node.env").write_text("secret-fixture")
    monkeypatch.setattr(build_payload, "__file__", str(module))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    archive(
        artifacts / "python",
        {"python/bin/python3.12": b"python-fixture", "python/share/terminfo/n/ALIAS": b"unused"},
    )
    archive(artifacts / "musl", {"lib/ld-musl-x86_64.so.1": b"loader-fixture"})
    archive(artifacts / "ffmpeg", {"ffmpeg-release/ffmpeg": b"ffmpeg-fixture"})
    with zipfile.ZipFile(artifacts / "websockets", "w") as wheel:
        wheel.writestr("websockets/__init__.py", "# pure Python\n")
    manifest = {
        name: {"sha256": hashlib.sha256((artifacts / name).read_bytes()).hexdigest()}
        for name in ("python", "musl", "ffmpeg", "websockets")
    }
    (artifacts / "manifest.json").write_text(json.dumps(manifest))
    return artifacts


def test_payload_has_only_runtime_and_omits_credentials_and_unused_terminfo(tmp_path, monkeypatch):
    artifacts = fixture(tmp_path, monkeypatch)
    target = tmp_path / "payload.tar"
    build_payload.build(artifacts, target)
    with tarfile.open(target) as tar:
        names = tar.getnames()
        assert "python/bin/python3.12" in names
        assert "lib/ld-musl-x86_64.so.1" in names
        assert "ffmpeg" in names and "nodekit/node.py" in names
        assert "adapters/ohmni/device.py" in names
        assert not any("terminfo/" in name or name.endswith(".env") for name in names)
        assert not any("/tools/" in name for name in names)


def test_payload_rejects_changed_binary_before_extracting(tmp_path, monkeypatch):
    artifacts = fixture(tmp_path, monkeypatch)
    (artifacts / "ffmpeg").write_bytes(b"modified")
    with pytest.raises(ValueError, match="verification failed: ffmpeg"):
        build_payload.build(artifacts, tmp_path / "payload.tar")
