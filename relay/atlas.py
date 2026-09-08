"""Durable incident spaces and source captures. Independent of motion authority."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_MEDIA_BYTES = 64 * 1024 * 1024
MAX_SPACE_BYTES = 2 * 1024 * 1024 * 1024
PRESENCE_AGE_MS = 90_000


class AtlasModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class Position(AtlasModel):
    latitude: float = Field(ge=-85, le=85)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float = Field(gt=0, le=10_000)
    timestamp: int = Field(gt=0)
    altitude: float | None = Field(default=None, ge=-500, le=15_000)
    heading: float | None = Field(default=None, ge=0, lt=360)


class NewSpace(AtlasModel):
    title: str = Field(min_length=3, max_length=100)
    description: str = Field(default="", max_length=2000)
    category: Literal["incident", "hazard", "community", "survey"] = "community"
    latitude: float = Field(ge=-85, le=85)
    longitude: float = Field(ge=-180, le=180)
    radius: int = Field(default=80, ge=20, le=500)
    place: str = Field(default="", max_length=120)


class Contributor(AtlasModel):
    contributor_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    name: str = Field(min_length=1, max_length=40)
    position: Position


class CaptureMetadata(AtlasModel):
    contributor_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["photo", "video", "panorama"]
    source: Literal["camera", "import"]
    captured_at: int = Field(gt=0)
    position: Position | None = None
    note: str = Field(default="", max_length=500)


class CaptureRequest(AtlasModel):
    cell_id: str = Field(pattern=r"^[0-9]{1,2}:[0-9]{1,2}$")
    note: str = Field(default="An additional viewpoint would help fill this area.", max_length=240)


class AtlasError(Exception):
    def __init__(self, detail: str, status: int = 400):
        self.detail, self.status = detail, status


def epoch_ms() -> int:
    return int(time.time() * 1000)


def local_xy(space: dict, position: dict) -> tuple[float, float]:
    dlon = (position["longitude"] - space["longitude"] + 180) % 360 - 180
    return (
        dlon * 111_320 * math.cos(math.radians(space["latitude"])),
        (position["latitude"] - space["latitude"]) * 111_320,
    )


def coverage(space: dict, captures: list[dict]) -> dict:
    """Report capture positions, never infer the surface seen by the camera."""
    size = space["radius"] / 5
    cells = {}
    for row in range(10):
        for col in range(10):
            x, y = (col - 4.5) * size, (row - 4.5) * size
            if math.hypot(x, y) <= space["radius"]:
                cells[f"{col}:{row}"] = {
                    "id": f"{col}:{row}",
                    "x": x,
                    "y": y,
                    "size": size,
                    "captures": 0,
                    "latitude": space["latitude"] + y / 111_320,
                    "longitude": (
                        space["longitude"]
                        + x / (111_320 * math.cos(math.radians(space["latitude"])))
                        + 180
                    )
                    % 360
                    - 180,
                }
    qualified = 0
    for capture in captures:
        position = capture["position"]
        if not position or capture["source"] != "camera":
            continue
        if position["accuracy"] > min(size / 2, 15):
            continue
        if abs(position["timestamp"] - capture["captured_at"]) > 10_000:
            continue
        x, y = local_xy(space, position)
        cell = cells.get(f"{math.floor(x / size + 5)}:{math.floor(y / size + 5)}")
        if cell:
            cell["captures"] += 1
            qualified += 1
    observed = sum(cell["captures"] > 0 for cell in cells.values())
    return {
        "cells": list(cells.values()),
        "observed": observed,
        "total": len(cells),
        "percent": round(observed / len(cells) * 100),
        "qualified_captures": qualified,
        "meaning": "Capture locations; not reconstructed surface completeness.",
    }


class AtlasStore:
    def __init__(self, root: Path, clock=epoch_ms):
        self.root, self.clock = root, clock
        self.lock = threading.RLock()
        root.mkdir(parents=True, exist_ok=True)
        self.media = root / "media"
        self.media.mkdir(exist_ok=True)
        self.db = sqlite3.connect(root / "atlas.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS spaces (
              id TEXT PRIMARY KEY, session TEXT NOT NULL,
              access_hash TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS spaces_session ON spaces(session);
            CREATE TABLE IF NOT EXISTS captures (
              id TEXT PRIMARY KEY, space TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS captures_space ON captures(space);
            CREATE TABLE IF NOT EXISTS presence (
              space TEXT NOT NULL, contributor TEXT NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(space, contributor));
            CREATE TABLE IF NOT EXISTS requests (
              space TEXT NOT NULL, cell TEXT NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(space, cell));
            CREATE TABLE IF NOT EXISTS reconstructions (
              id TEXT PRIMARY KEY, space TEXT NOT NULL, status TEXT NOT NULL,
              updated_at INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS reconstruction_space ON reconstructions(space);
        """)

    def close(self):
        self.db.close()

    def invitation(self, identifier: str) -> dict:
        """Issuing a replacement invitation invalidates the old shared credential."""
        token = secrets.token_urlsafe(32)
        with self.lock, self.db:
            self.db.execute(
                "UPDATE spaces SET access_hash=? WHERE id=?",
                (hashlib.sha256(token.encode()).hexdigest(), identifier),
            )
        return {"contributor_token": token}

    def create(self, session: str, value: NewSpace) -> dict:
        with self.lock, self.db:
            if (
                self.db.execute(
                    "SELECT count(*) FROM spaces WHERE session=?", (session,)
                ).fetchone()[0]
                >= 500
            ):
                raise AtlasError("This workspace has reached its 500-space limit.", 409)
            identifier, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
            data = {
                **value.model_dump(),
                "id": identifier,
                "created_at": self.clock(),
                "updated_at": self.clock(),
                "status": "active",
                "verification": "unverified",
            }
            self.db.execute(
                "INSERT INTO spaces VALUES (?,?,?,?)",
                (identifier, session, hashlib.sha256(token.encode()).hexdigest(), json.dumps(data)),
            )
            return {"space": data, "contributor_token": token}

    def check(self, identifier: str, *, session: str | None = None, token: str | None = None):
        with self.lock:
            row = self.db.execute(
                "SELECT session,access_hash,data FROM spaces WHERE id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise AtlasError("Space not found.", 404)
        if session is not None and row[0] == session:
            return json.loads(row[2])
        if token and secrets.compare_digest(row[1], hashlib.sha256(token.encode()).hexdigest()):
            return json.loads(row[2])
        raise AtlasError("This invitation does not grant access to the space.", 403)

    def list(self, session: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT data FROM spaces WHERE session=? ORDER BY rowid DESC LIMIT 500", (session,)
            ).fetchall()
            return [self.summary(json.loads(row[0])) for row in rows]

    def captures(self, identifier: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT data FROM captures WHERE space=? ORDER BY rowid DESC LIMIT 500",
                (identifier,),
            )
        ]

    def summary(self, space: dict) -> dict:
        captures = self.captures(space["id"])
        return {
            **space,
            "capture_count": len(captures),
            "coverage_percent": coverage(space, captures)["percent"],
            "contributors": len({item["contributor_id"] for item in captures}),
        }

    def detail(self, identifier: str) -> dict:
        with self.lock:
            space = json.loads(
                self.db.execute("SELECT data FROM spaces WHERE id=?", (identifier,)).fetchone()[0]
            )
            captures = self.captures(identifier)
            grid = coverage(space, captures)
            observed = {cell["id"] for cell in grid["cells"] if cell["captures"] > 0}
            requests = [
                json.loads(row[0])
                for row in self.db.execute("SELECT data FROM requests WHERE space=?", (identifier,))
            ]
            people = [
                json.loads(row[0])
                for row in self.db.execute("SELECT data FROM presence WHERE space=?", (identifier,))
            ]
            return {
                "space": self.summary(space),
                "captures": captures,
                "coverage": grid,
                "requests": [
                    {**item, "status": "captured" if item["cell_id"] in observed else "open"}
                    for item in requests
                ],
                "people": [
                    p for p in people if 0 <= self.clock() - p["updated_at"] <= PRESENCE_AGE_MS
                ],
                "reconstruction": self.reconstruction(identifier, len(captures)),
            }

    def update_status(self, identifier: str, status: Literal["active", "resolved"]) -> dict:
        with self.lock, self.db:
            data = self.detail(identifier)["space"]
            data.update(status=status, updated_at=self.clock())
            self.db.execute("UPDATE spaces SET data=? WHERE id=?", (json.dumps(data), identifier))
        return self.detail(identifier)

    def add_capture(
        self, identifier: str, metadata: CaptureMetadata, staged: Path, mime: str
    ) -> dict:
        size = staged.stat().st_size
        if not 0 < size <= MAX_MEDIA_BYTES:
            raise AtlasError("Choose a file between 1 byte and 64 MB.", 413)
        if metadata.captured_at > self.clock() + 60_000:
            raise AtlasError("The capture timestamp is in the future.")
        if (
            metadata.kind == "video"
            and not mime.startswith("video/")
            or metadata.kind != "video"
            and not mime.startswith("image/")
        ):
            raise AtlasError("The file does not match the capture type.")
        with self.lock, self.db:
            space = self.db.execute("SELECT data FROM spaces WHERE id=?", (identifier,)).fetchone()
            if not space or json.loads(space[0])["status"] != "active":
                raise AtlasError("This space is resolved. Reopen it before contributing.", 409)
            existing = self.captures(identifier)
            identifier_capture = str(uuid.uuid4())
            with staged.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            duplicate = next((c for c in existing if c["sha256"] == digest), None)
            if duplicate:
                return duplicate
            if len(existing) >= 500 or sum(c["bytes"] for c in existing) + size > MAX_SPACE_BYTES:
                raise AtlasError("This space has reached its capture storage limit.", 409)
            data = {
                **metadata.model_dump(),
                "id": identifier_capture,
                "uploaded_at": self.clock(),
                "mime": mime,
                "bytes": size,
                "sha256": digest,
            }
            target = self.media / identifier_capture
            staged.rename(target)
            try:
                self.db.execute(
                    "INSERT INTO captures VALUES (?,?,?)",
                    (identifier_capture, identifier, json.dumps(data)),
                )
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            return data

    def publish_presence(self, identifier: str, value: Contributor) -> dict:
        now = self.clock()
        if not 0 <= now - value.position.timestamp <= 30_000:
            raise AtlasError("Refresh your location before sharing it.")
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM presence WHERE space=? AND json_extract(data, '$.updated_at') < ?",
                (identifier, now - PRESENCE_AGE_MS),
            )
            people = self.db.execute(
                "SELECT contributor FROM presence WHERE space=?", (identifier,)
            ).fetchall()
            if len(people) >= 100 and (value.contributor_id,) not in people:
                raise AtlasError("This space has reached its contributor limit.", 409)
            data = {**value.model_dump(), "updated_at": now}
            self.db.execute(
                "INSERT OR REPLACE INTO presence VALUES (?,?,?)",
                (identifier, value.contributor_id, json.dumps(data)),
            )
            return data

    def reconstruction(self, identifier: str, capture_count: int) -> dict:
        row = self.db.execute(
            "SELECT data FROM reconstructions WHERE space=? ORDER BY rowid DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        if row:
            job = json.loads(row[0])
            return {key: value for key, value in job.items() if key != "sources"} | {
                "new_source_count": max(0, capture_count - job["source_count"]),
            }
        return {
            "status": "not_started",
            "source_count": capture_count,
            "detail": "Add overlapping views from different positions, then build the 3D atlas.",
        }

    def queue_reconstruction(self, identifier: str) -> dict:
        with self.lock, self.db:
            # BEGIN IMMEDIATE prevents two web workers from enqueueing the same space together.
            self.db.execute("BEGIN IMMEDIATE")
            current = self.reconstruction(identifier, 0)
            if current["status"] not in ("not_started", "ready", "failed"):
                return current
            captures = self.captures(identifier)
            if len(captures) < 3 and not any(c["kind"] == "video" for c in captures):
                raise AtlasError(
                    "Add at least three overlapping photos or a walking video first.", 409
                )
            if len(captures) > 120:
                raise AtlasError("This worker supports up to 120 source captures per build.", 409)
            count = self.db.execute(
                "SELECT count(*) FROM reconstructions WHERE space=?", (identifier,)
            ).fetchone()[0]
            pending = self.db.execute(
                "SELECT count(*) FROM reconstructions WHERE status NOT IN ('ready','failed')"
            ).fetchone()[0]
            if count >= 20 or pending >= 20:
                raise AtlasError("The reconstruction queue has reached its storage limit.", 409)
            job = {
                "id": str(uuid.uuid4()),
                "space_id": identifier,
                "status": "queued",
                "source_count": len(captures),
                "sources": captures,
                "created_at": self.clock(),
                "updated_at": self.clock(),
                "progress": 0,
                "detail": "Waiting for the reconstruction worker. Originals are preserved.",
                "engine": "COLMAP 4.2.0",
                "representation": "sparse_point_cloud",
                "coordinate_frame": "local_relative",
                "metric_scale": False,
            }
            self.db.execute(
                "INSERT INTO reconstructions VALUES (?,?,?,?,?)",
                (
                    job["id"],
                    identifier,
                    "queued",
                    job["updated_at"],
                    json.dumps(job),
                ),
            )
            return self.reconstruction(identifier, len(captures))

    def claim_reconstruction(self) -> dict | None:
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            # Expired jobs are failed, never silently rerun beside a possibly live worker.
            for job_id, raw in self.db.execute(
                "SELECT id,data FROM reconstructions "
                "WHERE status NOT IN ('queued','ready','failed') "
                "AND updated_at < ?",
                (self.clock() - 120_000,),
            ).fetchall():
                data = json.loads(raw) | {
                    "status": "failed",
                    "updated_at": self.clock(),
                    "detail": "The worker stopped reporting progress. Start a new build to retry.",
                }
                self.db.execute(
                    "UPDATE reconstructions SET status=?,updated_at=?,data=? WHERE id=?",
                    ("failed", self.clock(), json.dumps(data), job_id),
                )
            row = self.db.execute(
                "SELECT data FROM reconstructions WHERE status='queued' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job = json.loads(row[0]) | {
                "status": "preparing",
                "updated_at": self.clock(),
                "detail": "Checking source checksums and preparing camera views.",
                "progress": 5,
            }
            self.db.execute(
                "UPDATE reconstructions SET status=?,updated_at=?,data=? WHERE id=?",
                (job["status"], job["updated_at"], json.dumps(job), job["id"]),
            )
            return job

    def reconstruction_job(self, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT data FROM reconstructions WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise AtlasError("Reconstruction not found.", 404)
            return json.loads(row[0])

    def progress_reconstruction(self, job_id: str, **changes) -> bool:
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.reconstruction_job(job_id)
            if job["status"] in ("failed", "ready"):
                return False
            job.update(changes, updated_at=self.clock())
            self.db.execute(
                "UPDATE reconstructions SET status=?,updated_at=?,data=? WHERE id=?",
                (job["status"], job["updated_at"], json.dumps(job), job_id),
            )
            return True

    def leave(self, identifier: str, contributor: str):
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM presence WHERE space=? AND contributor=?", (identifier, contributor)
            )
        return {"sharing": False}

    def request_capture(self, identifier: str, value: CaptureRequest) -> dict:
        with self.lock, self.db:
            cells = self.detail(identifier)["coverage"]["cells"]
            cell = next((cell for cell in cells if cell["id"] == value.cell_id), None)
            if cell is None or cell["captures"] > 0:
                raise AtlasError("Select an area without a qualified capture.", 409)
            data = {
                **value.model_dump(),
                "created_at": self.clock(),
                "latitude": cell["latitude"],
                "longitude": cell["longitude"],
            }
            self.db.execute(
                "INSERT OR REPLACE INTO requests VALUES (?,?,?)",
                (identifier, value.cell_id, json.dumps(data)),
            )
            return data
