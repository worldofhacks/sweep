"""Versioned memory annotations and original soundtracks, separate from geometry inputs."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Literal, NamedTuple
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
    request_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


ACTIVE_ANALYSIS = {"running", "interrupted", "cancelling"}


class CancelMemory(AtlasModel):
    analysis_id: str = Field(pattern=r"^[0-9a-f-]{36}$")


class ReviewMemory(AtlasModel):
    revision: int = Field(ge=0)
    analysis_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")


class AnalysisAdmission(NamedTuple):
    context: dict
    analysis_id: str | None
    created: bool
    rejection: str | None = None
    replayed: bool = False


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
              CREATE TABLE IF NOT EXISTS memory_edits (
                space TEXT NOT NULL, capture TEXT NOT NULL, sequence INTEGER NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY(space,capture,sequence));
              CREATE TABLE IF NOT EXISTS memory_analysis_requests (
                space TEXT NOT NULL, capture TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, analysis_id TEXT, rejection TEXT,
                PRIMARY KEY(space,capture,request_id));
            """)
        from relay.memory_allowance import MemoryAllowance

        self.allowance = MemoryAllowance(self)

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

    def can_edit(self, space, capture, account_id, operator=False):
        if operator:
            return True
        if account_id is None:
            return False
        role = self.atlas.accounts.role(space, account_id)
        return role == "owner" or (
            role == "contributor" and capture.get("account_id") == account_id
        )

    def _editor(self, space, value, account_id):
        # None is the existing trusted internal/operator path, never a guest token.
        # HTTP routes must resolve signed identity before passing an account ID.
        if not self.can_edit(space, value["capture"], account_id, account_id is None):
            raise AtlasError("Only the Space owner or this capture's contributor can edit it.", 403)
        return account_id or "workspace-operator"

    def history(self, space, capture, before=201):
        with self.atlas.lock:
            self.capture(space, capture)
            return [
                json.loads(row[0])
                for row in self.atlas.db.execute(
                    "SELECT data FROM memory_edits WHERE space=? AND capture=? AND sequence<? "
                    "ORDER BY sequence DESC LIMIT 20",
                    (space, capture, before),
                )
            ]

    def _record(self, space, capture, value, actor, kind, change):
        sequence = (
            self.atlas.db.execute(
                "SELECT COALESCE(max(sequence),0) FROM memory_edits WHERE space=? AND capture=?",
                (space, capture),
            ).fetchone()[0]
            + 1
        )
        if sequence > 200:
            raise AtlasError("This memory has reached its 200-edit history limit.", 429)
        entry = {
            "sequence": sequence,
            "revision": value["revision"],
            "actor": actor,
            "changed_at": self.atlas.clock(),
            "kind": kind,
            **change,
        }
        self.atlas.db.execute(
            "INSERT INTO memory_edits VALUES (?,?,?,?)",
            (space, capture, sequence, json.dumps(entry, allow_nan=False)),
        )
        value["last_edit"] = {k: entry[k] for k in ("actor", "changed_at", "kind", "sequence")}

    @staticmethod
    def _editable(value, revision=None):
        if revision is not None and value["revision"] != revision:
            raise AtlasError("This memory changed in another window. Reload before saving.", 409)
        if (value.get("analysis") or {}).get("status") in ACTIVE_ANALYSIS:
            raise AtlasError(
                "Context analysis is running or awaiting completion confirmation. "
                "Your original is safe; wait or request a stop before retrying.",
                409,
            )

    def save(self, space, capture, request, *, account_id=None):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            actor = self._editor(space, value, account_id)
            self._editable(value, request.revision)
            notes = request.notes.model_dump()
            previous = value["notes"]
            value["notes"] = notes
            value["revision"] += 1
            self._record(
                space, capture, value, actor, "notes", {"previous": previous, "notes": notes}
            )
            value["review"] = None
            if value["analysis"]:
                value["analysis"]["status"] = "outdated"
            self._write(space, capture, value)
            return self.get(space, capture)

    def inspected(self, space, capture, inspection, *, account_id=None):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            self._editor(space, value, account_id)
            value["inspection"] = inspection
            self._write(space, capture, value)
            return self.get(space, capture)

    def add_asset(self, space, capture, staged, mime, metadata, *, account_id=None):
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
                actor = self._editor(space, value, account_id)
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
                    "added_by": actor,
                }
                target = self.media / asset["id"]
                staged.rename(target)
                self.atlas.db.execute(
                    "INSERT INTO memory_assets VALUES (?,?,?,?)",
                    (asset["id"], space, capture, json.dumps(asset)),
                )
                value["revision"] += 1
                self._record(space, capture, value, actor, "recording", {"asset": asset})
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
        admission = self.admit(space, capture, request)
        if admission.rejection:
            raise AtlasError(admission.rejection, 409)
        return admission.context

    def admit(self, space, capture, request, *, reserve=None):
        """Atomically deduplicate intent before reserving capacity or starting work.

        Return current context and a receipt identifying admitted or refused intent.
        Old keys never recreate old output or start a job, even after a newer analysis.
        The caller owns releasing any acquired capacity if commit/dispatch fails.
        """
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            inputs = request.model_dump(exclude={"request_id"})
            fingerprint = hashlib.sha256(
                json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if request.request_id:
                previous = self.atlas.db.execute(
                    "SELECT fingerprint,analysis_id,rejection FROM memory_analysis_requests "
                    "WHERE space=? AND capture=? AND request_id=?",
                    (space, capture, request.request_id),
                ).fetchone()
                if previous:
                    if previous[0] != fingerprint:
                        raise AtlasError(
                            "This request ID belongs to different analysis settings. "
                            "Recover it with its original settings.",
                            409,
                        )
                    return AnalysisAdmission(value, previous[1], False, previous[2], True)
            if (
                request.request_id
                and self.atlas.db.execute(
                    "SELECT count(*) FROM memory_analysis_requests WHERE space=? AND capture=?",
                    (space, capture),
                ).fetchone()[0]
                >= 200
            ):
                raise AtlasError(
                    "This capture has reached its 200 analysis-request limit. "
                    "Existing requests can still be recovered.",
                    429,
                )
            try:
                self._editable(value, request.revision)
                if request.audio_asset_id and not any(
                    a["id"] == request.audio_asset_id and a["role"] != "soundtrack"
                    for a in value["assets"]
                ):
                    raise AtlasError(
                        "Select an ambient recording or narration from this memory.", 422
                    )
            except AtlasError as error:
                if not request.request_id:
                    raise
                # Fence a refused intent too: a late copy must never start it later.
                self.atlas.db.execute(
                    "INSERT INTO memory_analysis_requests VALUES (?,?,?,?,?,?)",
                    (space, capture, request.request_id, fingerprint, None, str(error)),
                )
                return AnalysisAdmission(value, None, False, str(error))
            value["analysis"] = {
                "id": str(uuid.uuid4()),
                "status": "running",
                "started_at": self.atlas.clock(),
                "inputs": inputs,
                "request_id": request.request_id,
            }
            if (
                self.atlas.db.execute(
                    "SELECT count(*) FROM atlas_source_operations WHERE space=?", (space,)
                ).fetchone()[0]
                >= 256
            ):
                raise AtlasError("This Space has too many unfinished source operations.", 429)
            if reserve is not None and not reserve():
                raise AtlasError(
                    "Two context operations are running. Please try again shortly.", 429
                )
            # Keep the source pinned independently of the displayed context. A timeout,
            # future edit, or late result must never erase evidence of an active reader.
            self.atlas.db.execute(
                "INSERT INTO atlas_source_operations VALUES (?,?,?,?)",
                (value["analysis"]["id"], space, capture, "memory_analysis"),
            )
            value["review"] = None
            if request.request_id:
                self.atlas.db.execute(
                    "INSERT INTO memory_analysis_requests VALUES (?,?,?,?,?,?)",
                    (
                        space,
                        capture,
                        request.request_id,
                        fingerprint,
                        value["analysis"]["id"],
                        None,
                    ),
                )
            self._write(space, capture, value)
            return AnalysisAdmission(value, value["analysis"]["id"], True)

    def cancel(self, space, capture, request, *, account_id=None):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            actor = self._editor(space, value, account_id)
            current = value.get("analysis") or {}
            if current.get("id") != request.analysis_id:
                raise AtlasError("This analysis changed. Reload before requesting a stop.", 409)
            if current.get("status") in {"running", "interrupted"}:
                value["analysis"] = {
                    **current,
                    "status": "cancelling",
                    "cancel_requested_at": self.atlas.clock(),
                    "cancel_requested_by": actor,
                }
                self._write(space, capture, value)
            return value

    def review(self, space, capture, request, *, account_id=None):
        """Record an editor's review, never promote generated context into original evidence."""
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            value = self.get(space, capture)
            actor = self._editor(space, value, account_id)
            self._editable(value, request.revision)
            analysis = value.get("analysis") or {}
            if request.analysis_id and (
                analysis.get("id") != request.analysis_id
                or analysis.get("status") not in {"complete", "partial"}
            ):
                raise AtlasError("This draft changed. Reload before keeping this memory.", 409)
            previous = value.get("review") or {}
            if (
                previous.get("revision") == value["revision"]
                and previous.get("analysis_id") == request.analysis_id
                and previous.get("actor") == actor
            ):
                return value
            value["review"] = {
                "revision": value["revision"],
                "analysis_id": request.analysis_id,
                "reviewed_at": self.atlas.clock(),
                "actor": actor,
            }
            self._record(space, capture, value, actor, "review", {"review": value["review"]})
            self._write(space, capture, value)
            return value

    def require_analysis(self, capture, job_id):
        with self.atlas.lock:
            row = self.atlas.db.execute(
                "SELECT space FROM captures WHERE id=?", (capture,)
            ).fetchone()
            if not row:
                raise AtlasError("This source was removed.", 404)
            current = self.get(row[0], capture).get("analysis") or {}
            if current.get("id") != job_id or current.get("status") not in (
                "running",
                "interrupted",
            ):
                raise AtlasError("This source is no longer available for analysis.", 409)

    def finish(self, space, capture, job, result):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            # This callback is made only after the actual analysis/reader returns.
            self.atlas.db.execute(
                "DELETE FROM memory_provider_claims WHERE job=? AND EXISTS "
                "(SELECT 1 FROM atlas_source_operations WHERE id=? AND space=? AND capture=? "
                "AND kind='memory_analysis')",
                (job["id"], job["id"], space, capture),
            )
            self.atlas.db.execute(
                "DELETE FROM atlas_source_operations WHERE id=? AND space=? AND capture=? "
                "AND kind='memory_analysis'",
                (job["id"], space, capture),
            )
            removed = self.atlas.db.execute(
                "SELECT data FROM atlas_removals WHERE space=? AND capture=?", (space, capture)
            ).fetchone()
            if removed:
                if json.loads(removed[0]).get("analysis_id") == job["id"]:
                    self.atlas.db.execute(
                        "UPDATE atlas_removals SET data=json_set(data,"
                        "'$.analysis_released',json('true')) "
                        "WHERE space=? AND capture=?",
                        (space, capture),
                    )
                return None
            value = self.get(space, capture)
            current = value.get("analysis") or {}
            if current.get("id") == job["id"] and current.get("status") == "cancelling":
                value["analysis"] = {
                    **current,
                    "status": "cancelled",
                    "finished_at": self.atlas.clock(),
                    "warnings": [
                        "Analysis stopped. No new generated context was kept. "
                        "Already-sent provider requests cannot be recalled here."
                    ],
                }
                value["review"] = None
                self._write(space, capture, value)
                return self.get(space, capture)
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
