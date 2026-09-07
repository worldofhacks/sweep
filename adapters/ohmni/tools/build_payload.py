"""Build the verified Android 7.1 Ohmni runtime payload on a Python 3.12 host."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

_ARTIFACTS = ("python", "musl", "ffmpeg", "websockets")
_RUNTIME_RELAY_MODULES = (
    "__init__.py",
    "auth.py",
    "capabilities.py",
    "contracts.py",
    "intent_v1.py",
    "observations.py",
)


def build(artifacts: Path, output: Path) -> None:
    manifest = _manifest(artifacts)
    _verify(artifacts, manifest)
    root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="sweep-ohmni-") as directory:
        stage = Path(directory)
        _extract(artifacts / "python", stage)
        _extract(artifacts / "musl", stage / "musl")
        _extract(artifacts / "ffmpeg", stage / "ffmpeg-unpack")
        loader = stage / "musl" / "lib" / "ld-musl-x86_64.so.1"
        if not loader.is_file():
            raise ValueError("musl archive does not contain the x86_64 loader")
        (stage / "lib").mkdir()
        shutil.copy2(loader, stage / "lib" / loader.name)
        ffmpegs = list((stage / "ffmpeg-unpack").rglob("ffmpeg"))
        if len(ffmpegs) != 1:
            raise ValueError("ffmpeg archive must contain exactly one executable")
        shutil.copy2(ffmpegs[0], stage / "ffmpeg")
        site_packages = stage / "python" / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(artifacts / "websockets") as wheel:
            if any(name.endswith((".so", ".pyd")) for name in wheel.namelist()):
                raise ValueError("websockets must be a pure Python wheel")
            wheel.extractall(site_packages)
        _copy_runtime(root, stage)
        _smoke_import(stage, loader)
        for path in (stage / "musl", stage / "ffmpeg-unpack"):
            shutil.rmtree(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w") as archive:
            for path in sorted(stage.iterdir()):
                archive.add(path, arcname=path.name, filter=_normalized_tarinfo)


def _manifest(artifacts: Path) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads((artifacts / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("artifact manifest is unreadable") from error
    if set(raw) != set(_ARTIFACTS) or not all(
        isinstance(entry, dict) and isinstance(entry.get("sha256"), str) for entry in raw.values()
    ):
        raise ValueError("artifact manifest must name each required archive and SHA-256")
    return raw


def _verify(artifacts: Path, manifest: dict[str, dict[str, str]]) -> None:
    for name in _ARTIFACTS:
        path = artifacts / name
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != manifest[name]["sha256"]
        ):
            raise ValueError(f"artifact verification failed: {name}")


def _extract(source: Path, destination: Path) -> None:
    with tarfile.open(source, "r:*") as archive:
        archive.extractall(destination, filter="data")


def _normalized_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    if info.isdir():
        info.mode = 0o755
    elif info.isreg():
        info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


def _smoke_import(stage: Path, loader: Path) -> None:
    interpreter = stage / "python" / "bin" / "python3.12"
    if not interpreter.is_file():
        raise ValueError("Python archive does not contain the expected interpreter")
    try:
        subprocess.run(
            [
                str(loader),
                str(interpreter),
                "-I",
                "-c",
                "import sys; sys.path.insert(0, " + repr(str(stage)) + "); "
                "import adapters.ohmni.runtime; import relay.contracts",
            ],
            cwd=stage,
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ValueError("packaged musl Python could not start") from error
    except subprocess.CalledProcessError as error:
        raise ValueError(f"packaged runtime import failed: {error.stderr.strip()}") from error


def _copy_runtime(root: Path, stage: Path) -> None:
    ignored = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        "test_*",
        "tests",
        "tools",
        ".env",
        "*.env",
        ".git",
        "*.tar",
        "*.log",
    )
    (stage / "adapters").mkdir()
    shutil.copy2(root / "adapters" / "__init__.py", stage / "adapters" / "__init__.py")
    shutil.copytree(root / "adapters" / "ohmni", stage / "adapters" / "ohmni", ignore=ignored)
    (stage / "planner").mkdir()
    for name in ("__init__.py", "models.py"):
        shutil.copy2(root / "planner" / name, stage / "planner" / name)
    (stage / "relay").mkdir()
    for name in _RUNTIME_RELAY_MODULES:
        shutil.copy2(root / "relay" / name, stage / "relay" / name)
    shutil.copy2(root / "adapters" / "ohmni" / "run.sh", stage / "run.sh")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.artifacts, args.output)
