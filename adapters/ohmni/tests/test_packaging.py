from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from adapters.ohmni.tools import build_payload, stack


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
    for package, names in {
        "nodekit": (
            "__init__.py",
            "node.py",
            "device.py",
            "protocol.py",
            "peripherals.py",
            "telemetry.py",
        ),
        "adapters/ohmni": (
            "__init__.py",
            "__main__.py",
            "botshell.py",
            "camera.py",
            "device.py",
            "lidar.py",
            "odometry.py",
            "peripherals.py",
            "rplidar.py",
            "screen.py",
            "screen/index.html",
            "spike/__init__.py",
            "spike/rplidar_protocol.py",
        ),
    }.items():
        for name in names:
            path = root / package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# isolated runtime fixture\n")
    (root / "nodekit" / ".env").write_text("secret-fixture")
    (root / "nodekit" / "fake.py").write_text("# test-only device\n")
    (root / "nodekit" / "cli.py").write_text("# host-only launcher\n")
    (module.parent.parent / "run.sh").write_text("#!/system/bin/sh\n")
    (module.parent.parent / "node.env").write_text("secret-fixture")
    (module.parent.parent / "private-token.txt").write_text("secret-fixture")
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
        assert "nodekit/fake.py" not in names and "nodekit/cli.py" not in names
        assert "adapters/ohmni/private-token.txt" not in names


def test_payload_rejects_changed_binary_before_extracting(tmp_path, monkeypatch):
    artifacts = fixture(tmp_path, monkeypatch)
    (artifacts / "ffmpeg").write_bytes(b"modified")
    with pytest.raises(ValueError, match="verification failed: ffmpeg"):
        build_payload.build(artifacts, tmp_path / "payload.tar")


def test_source_upgrade_preserves_provisioned_binaries_and_private_configuration(
    tmp_path, monkeypatch
):
    fixture(tmp_path, monkeypatch)
    target = tmp_path / "sources.tar"
    build_payload.build_sources(target)
    with tarfile.open(target) as tar:
        names = tar.getnames()
        assert "nodekit/node.py" in names
        assert "adapters/ohmni/device.py" in names
        assert "run.sh" in names
        assert not any(name.startswith(("python/", "lib/")) for name in names)
        assert "ffmpeg" not in names
        assert not any(name.endswith((".env", ".key", ".pem")) for name in names)


def test_legacy_stack_entrypoint_only_refers_to_canonical_single_console(capsys):
    assert stack.main() == 2
    message = capsys.readouterr().out
    assert "tools/ground_runtime.py" in message
    assert "5173" in message
    assert "No service or robot was started, stopped, or reconfigured" in message
