"""Build an uncompressed adb payload on the host; never package .env or repository data."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path


def build(artifacts: Path, output: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((artifacts / "manifest.json").read_text())
    for name in ("python", "musl", "ffmpeg", "websockets"):
        if hashlib.sha256((artifacts / name).read_bytes()).hexdigest() != manifest[name]["sha256"]:
            raise ValueError(f"artifact verification failed: {name}")
    with tempfile.TemporaryDirectory(prefix="sweep-ohmni-") as folder:
        stage = Path(folder)
        for name in ("python", "musl", "ffmpeg"):
            extracted = stage / f"unpack-{name}"
            with tarfile.open(artifacts / name, "r:*", ignore_zeros=True) as archive:
                # The upstream terminfo tree has case-distinct aliases that become
                # self-referential symlinks on the host's default macOS filesystem.
                # This headless node does not use curses or terminal capabilities.
                members = (
                    member
                    for member in archive.getmembers()
                    if not member.name.removeprefix("./").startswith("python/share/terminfo/")
                )
                archive.extractall(extracted, members=members, filter="data")
        shutil.move(stage / "unpack-python" / "python", stage / "python")
        (stage / "lib").mkdir()
        shutil.copy2(
            stage / "unpack-musl" / "lib" / "ld-musl-x86_64.so.1",
            stage / "lib" / "ld-musl-x86_64.so.1",
        )
        binaries = list((stage / "unpack-ffmpeg").rglob("ffmpeg"))
        if len(binaries) != 1:
            raise ValueError("expected one ffmpeg binary in the static archive")
        shutil.copy2(binaries[0], stage / "ffmpeg")
        packages = stage / "python" / "lib" / "python3.12" / "site-packages"
        packages.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(artifacts / "websockets") as wheel:
            # A pure wheel is mandatory: Android cannot load glibc extension modules.
            if any(name.endswith((".so", ".pyd")) for name in wheel.namelist()):
                raise ValueError("websockets must be the py3-none-any wheel")
            wheel.extractall(packages)
        ignore = shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "tests",
            "vectors",
            "tools",
            ".env",
            "*.env",
            ".git",
            "artifacts",
            "*.tar",
            "*.log",
        )
        shutil.copytree(root / "nodekit", stage / "nodekit", ignore=ignore)
        (stage / "adapters").mkdir()
        (stage / "adapters" / "__init__.py").touch()
        shutil.copytree(root / "adapters" / "ohmni", stage / "adapters" / "ohmni", ignore=ignore)
        shutil.copy2(root / "adapters" / "ohmni" / "run.sh", stage / "run.sh")
        for name in ("unpack-python", "unpack-musl", "unpack-ffmpeg"):
            shutil.rmtree(stage / name)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Plain tar: robot toybox handles it; busybox's gzip path crashes on this OS.
        with tarfile.open(output, "w") as archive:
            for path in sorted(stage.iterdir()):
                archive.add(path, arcname=path.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.artifacts, args.output)
