"""Fetch and verify the four Android runtime artifacts described by an operator manifest.

No package manager executes on the robot. Pass HTTPS URLs plus reviewed SHA256 hashes
for python-build-standalone 3.12 x86_64 musl, Alpine musl APK, static ffmpeg x86_64,
and a pure-Python websockets wheel. Exact upstream URLs/hashes are input, never guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

NAMES = ("python", "musl", "ffmpeg", "websockets")


def fetch(manifest: dict, destination: Path) -> None:
    if set(manifest) != set(NAMES):
        raise ValueError("manifest must contain exactly python, musl, ffmpeg, websockets")
    destination.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        entry = manifest[name]
        url, digest = entry["url"], entry["sha256"]
        if not url.startswith("https://") or not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError(f"{name}: HTTPS URL and SHA256 are required")
        target = destination / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        temporary = destination / f"{name}.partial"
        try:
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                temporary.open("wb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                raise ValueError(f"{name}: SHA256 mismatch")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    fetch(json.loads(args.manifest.read_text()), args.destination)


if __name__ == "__main__":
    main()
