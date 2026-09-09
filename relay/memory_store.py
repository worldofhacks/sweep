"""Versioned memory annotations and original soundtracks, separate from geometry inputs."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from relay.atlas import AtlasError, AtlasModel


class MemoryLocation(AtlasModel):
    latitude: float = Field(ge=-85, le=85)
    longitude: float = Field(ge=-180, le=180)


class MemoryNotes(AtlasModel):
    description: str = Field(default="", max_length=2000)
    feeling: str = Field(default="", max_length=500)
    occurred_at: str | None = Field(default=None, max_length=40)
    location: MemoryLocation | None = None
    music_title: str = Field(default="", max_length=200)
    music_url: str = Field(default="", max_length=1000)

    @field_validator("occurred_at")
    @classmethod
    def timestamp(cls, value):
        if value is None:
            return None
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.year < 1940 or instant > datetime.now(UTC):
            raise ValueError("Use a past date/time with its UTC offset, from 1940 onward.")
        return instant.isoformat()

    @field_validator("music_url")
    @classmethod
    def music_link(cls, value):
        if not value:
            return value
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.hostname
            not in {
                "open.spotify.com",
                "music.apple.com",
                "www.youtube.com",
                "youtube.com",
                "youtu.be",
            }
        ):
            raise ValueError("Use an HTTPS Spotify, Apple Music, or YouTube link.")
        return value


class SaveMemory(AtlasModel):
    revision: int = Field(ge=0)
    notes: MemoryNotes


class MemoryAsset(AtlasModel):
    title: str = Field(min_length=1, max_length=120)
    role: Literal["ambient", "narration", "soundtrack"]
    rights_confirmed: Literal[True]


class AnalyzeMemory(AtlasModel):
    revision: int = Field(ge=0)
    weather: bool = False
    ai: bool = False
    audio_asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class ReviewMemory(AtlasModel):
    revision: int = Field(ge=0)
    analysis_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class MemoryStore:
    def __init__(self, atlas):
        self.atlas = atlas
        self.media = atlas.root / "memory-media"
        self.media.mkdir(exist_ok=True)
        with atlas.lock, atlas.db:
            atlas.db.executescript("""
              CREATE TABLE IF NOT EXISTS memory_contexts (
                space TEXT NOT NULL, capture TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(space,capture));
              CREATE TABLE IF NOT EXISTS memory_assets (
                id TEXT PRIMARY KEY, space TEXT NOT NULL,
                capture TEXT NOT NULL, data TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS memory_assets_capture ON memory_assets(space,capture);
            """)

    def capture(self, space, capture):
        with self.atlas.lock:
            row = self.atlas.db.execute(
                "SELECT data FROM captures WHERE space=? AND id=?", (space, capture)
            ).fetchone()
            if not row:
                raise AtlasError("Capture not found in this space.", 404)
            return json.loads(row[0])

    def get(self, space, capture):
        with self.atlas.lock:
            original = self.capture(space, capture)
            row = self.atlas.db.execute(
                "SELECT data FROM memory_contexts WHERE space=? AND capture=?", (space, capture)
            ).fetchone()
            context = (
                json.loads(row[0])
                if row
                else {
                    "revision": 0,
                    "notes": MemoryNotes().model_dump(),
                    "inspection": None,
                    "analysis": None,
                    "review": None,
                }
            )
            analysis = context.get("analysis")
            if (
                analysis
                and analysis["status"] == "running"
                and (self.atlas.clock() - analysis["started_at"] > 180_000)
            ):
                context["analysis"] = {**analysis, "status": "interrupted"}
            return {
                **context,
                "capture": original,
                "assets": [
                    json.loads(row[0])
                    for row in self.atlas.db.execute(
                        "SELECT data FROM memory_assets WHERE space=? AND capture=? ORDER BY rowid",
                        (space, capture),
                    )
                ],
            }

    def _write(self, space, capture, value):
        data = {k: v for k, v in value.items() if k not in {"capture", "assets"}}
        self.atlas.db.execute(
            "INSERT OR REPLACE INTO memory_contexts VALUES (?,?,?)",
            (space, capture, json.dumps(data, allow_nan=False)),
        )

    @staticmethod
    def _editable(value, revision=None):
        if revision is not None and value["revision"] != revision:
            raise AtlasError("This memory changed in another window. Reload before saving.", 409)
        if (value.get("analysis") or {}).get("status") == "running":
            raise AtlasError(
                "Context analysis is running. Your original is safe; try again shortly.", 409
            )

    def save(self, space, capture, request):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            self._editable(value, request.revision)
            value["notes"] = request.notes.model_dump()
            value["revision"] += 1
            value["review"] = None
            if value["analysis"]:
                value["analysis"]["status"] = "outdated"
            self._write(space, capture, value)
            return self.get(space, capture)

    def inspected(self, space, capture, inspection):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            value["inspection"] = inspection
            self._write(space, capture, value)
            return self.get(space, capture)

    def add_asset(self, space, capture, staged, mime, metadata):
        size = staged.stat().st_size
        if not 0 < size <= 64 * 1024 * 1024:
            raise AtlasError("Choose an audio or video original smaller than 64 MB.", 413)
        with staged.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        target = None
        try:
            with self.atlas.lock, self.atlas.db:
                self.atlas.db.execute("BEGIN IMMEDIATE")
                value = self.get(space, capture)
                self._editable(value)
                for asset in value["assets"]:
                    if asset["sha256"] == digest and asset["role"] == metadata.role:
                        return asset
                used = self.atlas.db.execute(
                    "SELECT COALESCE(sum(json_extract(data,'$.bytes')),0) "
                    "FROM memory_assets WHERE space=?",
                    (space,),
                ).fetchone()[0]
                if len(value["assets"]) >= 8 or used + size > 256 * 1024 * 1024:
                    raise AtlasError(
                        "Limit reached: eight memory tracks per capture, 256 MB per space.", 409
                    )
                asset = {
                    **metadata.model_dump(),
                    "id": str(uuid.uuid4()),
                    "mime": mime,
                    "bytes": size,
                    "sha256": digest,
                    "added_at": self.atlas.clock(),
                }
                target = self.media / asset["id"]
                staged.rename(target)
                self.atlas.db.execute(
                    "INSERT INTO memory_assets VALUES (?,?,?,?)",
                    (asset["id"], space, capture, json.dumps(asset)),
                )
                value["revision"] += 1
                value["review"] = None
                if value["analysis"]:
                    value["analysis"]["status"] = "outdated"
                self._write(space, capture, value)
                return asset
        except BaseException:
            if target is not None:
                target.unlink(missing_ok=True)
            raise

    def begin(self, space, capture, request):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            self._editable(value, request.revision)
            if request.audio_asset_id and not any(
                a["id"] == request.audio_asset_id and a["role"] != "soundtrack"
                for a in value["assets"]
            ):
                raise AtlasError("Select an ambient recording or narration from this memory.", 422)
            value["analysis"] = {
                "id": str(uuid.uuid4()),
                "status": "running",
                "started_at": self.atlas.clock(),
                "inputs": request.model_dump(),
            }
            value["review"] = None
            self._write(space, capture, value)
            return value

    def review(self, space, capture, request):
        """Record an owner's review, never promote generated context into original evidence."""
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            self._editable(value, request.revision)
            analysis = value.get("analysis") or {}
            if request.analysis_id and (
                analysis.get("id") != request.analysis_id
                or analysis.get("status") not in {"complete", "partial"}
            ):
                raise AtlasError("This draft changed. Reload before keeping this memory.", 409)
            value["review"] = {
                "revision": value["revision"],
                "analysis_id": request.analysis_id,
                "reviewed_at": self.atlas.clock(),
            }
            self._write(space, capture, value)
            return value

    def finish(self, space, capture, job, result):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            current = value.get("analysis") or {}
            if (
                current.get("id") == job["id"]
                and current.get("status") in {"running", "interrupted"}
                and value["revision"] == job["inputs"]["revision"]
            ):
                value["analysis"] = {**job, **result, "finished_at": self.atlas.clock()}
                value["review"] = None
                if result.get("inspection"):
                    value["inspection"] = result["inspection"]
                self._write(space, capture, value)
            return self.get(space, capture)
