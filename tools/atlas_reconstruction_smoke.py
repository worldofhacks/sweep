"""Upload real example photos to a local preview and queue a reconstruction.

Only targets the loopback preview. Data is marked DEMO and given no asserted GPS fix.
Run the worker separately, then use --verify-space SPACE_ID to verify the artifact.
"""

import argparse
import hashlib
import json
import struct
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8177)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--verify-space")
    args = parser.parse_args()
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=90) as client:
        bootstrap = client.get("/relay-bootstrap.json").json()["relay"]
        client.headers["Authorization"] = f"Bearer {bootstrap['token']}"
        base = f"/api/sessions/{bootstrap['sessionId']}/atlas/spaces"
        if args.verify_space:
            response = client.get(f"{base}/{args.verify_space}")
            response.raise_for_status()
            detail = response.json()
            job = detail["reconstruction"]
            assert job["status"] == "ready", job
            assert job["registered_views"] >= 3 and job["points"] >= 50, job
            assert detail["coverage"]["observed"] == 0, "Imports must not invent GPS coverage"
            artifact = client.get(
                f"{base}/{args.verify_space}/reconstruction/{job['id']}/cloud.glb"
            )
            artifact.raise_for_status()
            assert hashlib.sha256(artifact.content).hexdigest() == job["artifact_sha256"]
            magic, version, length = struct.unpack("<4sII", artifact.content[:12])
            assert (magic, version, length) == (b"glTF", 2, len(artifact.content))
            print(json.dumps(job, indent=2))
            return
        if not args.images:
            parser.error("Pass --images DIRECTORY or --verify-space SPACE_ID")
        files = sorted(
            p for p in args.images.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")
        )
        if not 3 <= len(files) <= 120:
            parser.error("Use 3 to 120 real overlapping example photos")
        response = client.post(
            base,
            json={
                "title": "Demo · Fountain reconstruction",
                "category": "survey",
                "description": "COLMAP example photos, imported for reconstruction validation. "
                "This is not a live incident or a GPS-verified survey.",
                "latitude": 0,
                "longitude": 0,
                "radius": 80,
                "place": "COLMAP example dataset · geographic location not asserted",
            },
        )
        response.raise_for_status()
        identifier = response.json()["space"]["id"]
        for file in files:
            metadata = {
                "contributor_id": "example-dataset",
                "name": "COLMAP example dataset",
                "kind": "photo",
                "source": "import",
                "captured_at": round(file.stat().st_mtime * 1000),
                "position": None,
                "note": "Official COLMAP example; no capture-time GPS asserted.",
            }
            with file.open("rb") as handle:
                upload = client.post(
                    f"{base}/{identifier}/captures",
                    content=handle,
                    headers={
                        "Content-Type": "image/png" if file.suffix == ".png" else "image/jpeg",
                        "X-Sweep-Capture": json.dumps(metadata),
                    },
                )
            upload.raise_for_status()
            print(f"Uploaded {file.name}", flush=True)
        queued = client.post(f"{base}/{identifier}/reconstruction", json={})
        queued.raise_for_status()
        print(
            json.dumps(
                {"space_id": identifier, "job": queued.json(), "at": int(time.time())}, indent=2
            )
        )


if __name__ == "__main__":
    main()
