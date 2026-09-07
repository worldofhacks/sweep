from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from .tools import build_payload


def _archive(path: Path, entries: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    module = root / "adapters" / "ohmni" / "tools" / "build_payload.py"
    module.parent.mkdir(parents=True)
    (root / "adapters" / "__init__.py").write_text("")
    (root / "adapters" / "ohmni").mkdir(exist_ok=True)
    for name in ("runtime.py", "device.py", "run.sh", "node.env", "test_runtime.py"):
        (root / "adapters" / "ohmni" / name).write_text(
            "secret" if name == "node.env" else "# runtime\n"
        )
    (root / "planner").mkdir()
    for name in ("__init__.py", "models.py"):
        (root / "planner" / name).write_text("# runtime\n")
    (root / "relay").mkdir()
    for name in build_payload._RUNTIME_RELAY_MODULES:
        (root / "relay" / name).write_text("# runtime\n")
    monkeypatch.setattr(build_payload, "__file__", str(module))
    monkeypatch.setattr(build_payload, "_smoke_import", lambda _stage, _loader: None)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _archive(artifacts / "python", {"python/bin/python3.12": b"python"})
    _archive(artifacts / "musl", {"lib/ld-musl-x86_64.so.1": b"loader"})
    _archive(artifacts / "ffmpeg", {"release/ffmpeg": b"ffmpeg"})
    with zipfile.ZipFile(artifacts / "websockets", "w") as wheel:
        wheel.writestr("websockets/__init__.py", "# pure\n")
    manifest = {
        name: {"sha256": hashlib.sha256((artifacts / name).read_bytes()).hexdigest()}
        for name in build_payload._ARTIFACTS
    }
    (artifacts / "manifest.json").write_text(json.dumps(manifest))
    return artifacts


def test_payload_carries_the_musl_runtime_and_excludes_private_node_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _fixture(tmp_path, monkeypatch)
    output = tmp_path / "ohmni-runtime.tar"

    build_payload.build(artifacts, output)

    with tarfile.open(output) as archive:
        names = archive.getnames()
    assert "python/bin/python3.12" in names
    assert "lib/ld-musl-x86_64.so.1" in names
    assert "adapters/ohmni/runtime.py" in names
    assert "planner/models.py" in names
    assert "relay/contracts.py" in names
    assert "run.sh" in names
    assert not any(name.endswith("node.env") or "test_runtime" in name for name in names)


def test_payload_refuses_an_artifact_that_changed_after_manifest_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _fixture(tmp_path, monkeypatch)
    (artifacts / "ffmpeg").write_bytes(b"changed")

    with pytest.raises(ValueError, match="artifact verification failed: ffmpeg"):
        build_payload.build(artifacts, tmp_path / "ohmni-runtime.tar")


def test_payload_smoke_import_uses_the_packaged_musl_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    loader = stage / "lib" / "ld-musl-x86_64.so.1"
    interpreter = stage / "python" / "bin" / "python3.12"
    loader.parent.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    loader.write_text("")
    interpreter.write_text("")
    calls: list[tuple[list[str], Path]] = []

    def run(command: list[str], **kwargs: object) -> None:
        calls.append((command, kwargs["cwd"]))  # type: ignore[arg-type]

    monkeypatch.setattr(build_payload.subprocess, "run", run)

    build_payload._smoke_import(stage, loader)

    assert calls == [
        (
            [
                str(loader),
                str(interpreter),
                "-I",
                "-c",
                "import sys; sys.path.insert(0, " + repr(str(stage)) + "); "
                "import adapters.ohmni.runtime; import relay.contracts",
            ],
            stage,
        )
    ]
