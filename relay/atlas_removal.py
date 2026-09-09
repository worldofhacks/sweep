"""Source withdrawal with durable, retryable local cleanup; not a backup-erasure claim."""

import hashlib
import json
import shutil
import sqlite3
import uuid

from fastapi import BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from relay.atlas import AtlasError, AtlasModel
from relay.memory_store import ACTIVE_ANALYSIS


class RemoveCapture(AtlasModel):
    confirmation: str = Field(pattern=r"^[0-9a-f]{64}$")


class AtlasRemoval:
    def __init__(self, atlas):
        self.atlas = atlas
        with atlas.lock, atlas.db:
            atlas.db.execute("""CREATE TABLE IF NOT EXISTS atlas_removals (
                capture TEXT PRIMARY KEY, space TEXT NOT NULL, digest TEXT NOT NULL,
                account TEXT, data TEXT NOT NULL)""")
            atlas.db.execute(
                "CREATE INDEX IF NOT EXISTS atlas_removal_space ON atlas_removals(space)"
            )
            atlas.db.execute(
                "CREATE INDEX IF NOT EXISTS atlas_removal_pending ON atlas_removals "
                "(COALESCE(json_extract(data,'$.last_attempt'),0)) "
                "WHERE json_extract(data,'$.state')='cleanup_pending'"
            )
            atlas.db.execute("""CREATE TABLE IF NOT EXISTS atlas_build_sources (
                job TEXT NOT NULL, space TEXT NOT NULL, capture TEXT NOT NULL,
                PRIMARY KEY(job,capture))""")
            atlas.db.execute(
                "CREATE INDEX IF NOT EXISTS atlas_build_source_capture "
                "ON atlas_build_sources(space,capture)"
            )
            atlas.db.execute(
                "INSERT OR IGNORE INTO atlas_build_sources "
                "SELECT r.id,r.space,json_extract(s.value,'$.id') FROM reconstructions r, "
                "json_each(r.data,'$.sources') s"
            )
            atlas.db.execute("""CREATE TABLE IF NOT EXISTS atlas_source_operations (
                id TEXT PRIMARY KEY, space TEXT NOT NULL, capture TEXT, kind TEXT NOT NULL)""")
            atlas.db.execute(
                "CREATE INDEX IF NOT EXISTS atlas_operation_capture "
                "ON atlas_source_operations(space,capture)"
            )

    def begin_operation(self, space, capture, kind):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            self.atlas._space(space)
            if capture is not None:
                self.atlas.memories.capture(space, capture)
            if (
                self.atlas.db.execute(
                    "SELECT count(*) FROM atlas_source_operations WHERE space=?", (space,)
                ).fetchone()[0]
                >= 256
            ):
                raise AtlasError(
                    "This Space has too many unfinished transfers. Please try later.", 429
                )
            identifier = str(uuid.uuid4())
            self.atlas.db.execute(
                "INSERT INTO atlas_source_operations VALUES (?,?,?,?)",
                (identifier, space, capture, kind),
            )
            return identifier

    def finish_operation(self, identifier):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("DELETE FROM atlas_source_operations WHERE id=?", (identifier,))

    def _receipt(self, space, capture):
        row = self.atlas.db.execute(
            "SELECT account,data FROM atlas_removals WHERE space=? AND capture=?", (space, capture)
        ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def _authorize(self, space, capture, account, operator):
        if not self.atlas.memories.can_edit(space, capture, account, operator):
            raise AtlasError(
                "Only this capture's contributor or the Space owner can remove it.", 403
            )

    @staticmethod
    def public(receipt):
        return {
            key: receipt[key]
            for key in (
                "capture_id",
                "requested_at",
                "requested_by",
                "completed_at",
                "state",
                "recordings",
                "builds",
            )
        } | {"analysis_pending": not receipt.get("analysis_released", False)}

    def _inventory(self, space, capture):
        memory = self.atlas.memories.get(space, capture)
        jobs = [
            json.loads(row[0])
            for row in self.atlas.db.execute(
                "SELECT r.data FROM reconstructions r JOIN atlas_build_sources s ON s.job=r.id "
                "WHERE s.space=? AND s.capture=?",
                (space, capture),
            )
        ]
        dates = self.atlas.timeline.history(space, capture)
        signature = {
            "capture": capture,
            "revision": memory["revision"],
            "analysis": (memory.get("analysis") or {}).get("id"),
            "date_revision": dates[0]["revision"] if dates else 0,
            "assets": sorted(a["id"] for a in memory["assets"]),
            "builds": sorted(job["id"] for job in jobs),
        }
        confirmation = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        return memory, jobs, confirmation

    def receipts(self, space, account=None, operator=False, before=1001):
        """Stable, bounded pages; contributor receipts never expose somebody else's activity."""
        with self.atlas.lock:
            self.atlas._space(space)
            role = "owner" if operator else self.atlas.accounts.role(space, account)
            if role not in ("owner", "contributor"):
                raise AtlasError("Removal receipts are available to owners and contributors.", 403)
            scope = "space=?" + (" AND account=?" if role == "contributor" else "")
            values = (space, account) if role == "contributor" else (space,)
            # Scope-local ordinals reveal no global activity counter. This bounded
            # archive has at most 1,000 immutable rows; later inserts append ranks.
            rows = self.atlas.db.execute(
                "SELECT ordinal,data FROM (SELECT row_number() OVER (ORDER BY rowid) "
                f"AS ordinal,data FROM atlas_removals WHERE {scope}) WHERE ordinal<? "
                "ORDER BY ordinal DESC LIMIT 21",
                (*values, before),
            ).fetchall()
            counts = dict(
                self.atlas.db.execute(
                    f"SELECT json_extract(data,'$.state'),count(*) FROM atlas_removals "
                    f"WHERE {scope} GROUP BY json_extract(data,'$.state')",
                    values,
                ).fetchall()
            )
            return {
                "receipts": [self.public(json.loads(row[1])) for row in rows[:20]],
                "next_before": rows[19][0] if len(rows) > 20 else None,
                "pending": counts.get("cleanup_pending", 0),
                "completed": counts.get("local_removed", 0),
                "scope": "space" if role == "owner" else "own",
            }

    def preview(self, space, capture, account=None, operator=False):
        with self.atlas.lock:
            receipt = self._receipt(space, capture)
            if receipt:
                self._authorize(space, {"account_id": receipt[0]}, account, operator)
                return self.public(receipt[1])
            memory, jobs, confirmation = self._inventory(space, capture)
            self._authorize(space, memory["capture"], account, operator)
            return {
                "capture_id": capture,
                "state": "preview",
                "confirmation": confirmation,
                "recordings": len(memory["assets"]),
                "builds": len(jobs),
                "analysis_pending": (memory.get("analysis") or {}).get("status") in ACTIVE_ANALYSIS,
            }

    def remove(self, space, capture, confirmation, account=None, operator=False):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            receipt = self._receipt(space, capture)
            if receipt:
                self._authorize(space, {"account_id": receipt[0]}, account, operator)
                return self.public(receipt[1])
            memory, jobs, current = self._inventory(space, capture)
            self._authorize(space, memory["capture"], account, operator)
            if confirmation != current:
                raise AtlasError("This memory changed. Review what will be removed again.", 409)
            if (
                self.atlas.db.execute(
                    "SELECT count(*) FROM atlas_removals WHERE space=?", (space,)
                ).fetchone()[0]
                >= 1000
            ):
                raise AtlasError(
                    "This Space has reached its removal-receipt limit. Contact its operator.", 409
                )
            receipt = {
                "capture_id": capture,
                "requested_at": self.atlas.clock(),
                "requested_by": "workspace-operator" if operator else account,
                "completed_at": None,
                "state": "cleanup_pending",
                "recordings": len(memory["assets"]),
                "builds": len(jobs),
                "assets": [a["id"] for a in memory["assets"]],
                "jobs": [j["id"] for j in jobs],
                "analysis_id": (memory.get("analysis") or {}).get("id"),
                "analysis_released": (memory.get("analysis") or {}).get("status")
                not in ACTIVE_ANALYSIS,
            }
            for job in jobs:
                # A queued job has no worker. Other jobs require an explicit supervisor
                # acknowledgement, never a lease timeout or inferred process death.
                released = job["status"] == "queued" or job.get("worker_released", False)
                withdrawn = {
                    "id": job["id"],
                    "space_id": space,
                    "status": "failed",
                    "source_count": 0,
                    "sources": [],
                    "created_at": job["created_at"],
                    "updated_at": self.atlas.clock(),
                    "progress": 0,
                    "detail": "A source was removed. Build again with the remaining captures.",
                    "source_withdrawn": True,
                    "worker_released": released,
                }
                self.atlas.db.execute(
                    "UPDATE reconstructions SET status='failed',updated_at=?,data=? WHERE id=?",
                    (self.atlas.clock(), json.dumps(withdrawn), job["id"]),
                )
                self.atlas.db.execute(
                    "DELETE FROM surface_requests WHERE space=? AND job=?", (space, job["id"])
                )
                self.atlas.db.execute(
                    "DELETE FROM capture_responses WHERE space=? "
                    "AND json_extract(request,'$.job_id')=?",
                    (space, job["id"]),
                )
            self.atlas.db.execute(
                "INSERT INTO atlas_removals VALUES (?,?,?,?,?)",
                (
                    capture,
                    space,
                    memory["capture"]["sha256"],
                    memory["capture"].get("account_id"),
                    json.dumps(receipt),
                ),
            )
            for table in (
                "memory_contexts",
                "memory_assets",
                "memory_edits",
                "memory_analysis_requests",
                "atlas_capture_dates",
                "capture_responses",
            ):
                self.atlas.db.execute(
                    f"DELETE FROM {table} WHERE space=? AND capture=?", (space, capture)
                )
            self.atlas.db.execute("DELETE FROM captures WHERE space=? AND id=?", (space, capture))
            return self.public(receipt)

    def reject_reimport(self, space, digest):
        if self.atlas.db.execute(
            "SELECT 1 FROM atlas_removals WHERE space=? AND digest=?", (space, digest)
        ).fetchone():
            raise AtlasError(
                "This original was removed from this Space. Automatic re-upload is blocked.",
                409,
                code="capture_removed",
            )

    def _path(self, folder, identifier):
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError("Invalid cleanup identifier")
        parent = self.atlas.root / folder
        if parent.is_symlink() or parent.resolve().parent != self.atlas.root.resolve():
            raise ValueError("Cleanup directory is not owned by this Atlas store")
        return parent / identifier

    def cleanup(self):
        """Retry a bounded batch. Access is already revoked before filesystem operations."""
        with self.atlas.lock:
            captures = [
                row[0]
                for row in self.atlas.db.execute(
                    "SELECT capture FROM atlas_removals "
                    "WHERE json_extract(data,'$.state')='cleanup_pending' "
                    "ORDER BY COALESCE(json_extract(data,'$.last_attempt'),0) LIMIT 20"
                )
            ]
        eligible = []
        for capture in captures:
            try:
                with self.atlas.lock:
                    row = self.atlas.db.execute(
                        "SELECT data FROM atlas_removals WHERE capture=?", (capture,)
                    ).fetchone()
                    receipt = json.loads(row[0])
                if receipt["state"] != "cleanup_pending":
                    continue
                # Never hold Atlas's database lock while deleting a potentially large
                # generated directory. Withdrawn sources and released jobs are immutable.
                self._path("media", capture).unlink(missing_ok=True)
                for asset in receipt["assets"]:
                    self._path("memory-media", asset).unlink(missing_ok=True)
                with self.atlas.lock:
                    pending = not receipt.get("analysis_released", False) or bool(
                        self.atlas.db.execute(
                            "SELECT 1 FROM atlas_source_operations o "
                            "JOIN atlas_removals r ON r.space=o.space "
                            "WHERE r.capture=? AND (o.capture=r.capture OR o.capture IS NULL)",
                            (capture,),
                        ).fetchone()
                    )
                for job_id in receipt["jobs"]:
                    job = self.atlas.reconstruction_job(job_id)
                    if not job.get("worker_released"):
                        pending = True
                        continue
                    path = self._path("reconstructions", job_id)
                    if path.is_symlink():
                        path.unlink()
                    elif path.exists():
                        shutil.rmtree(path)
                    with self.atlas.lock, self.atlas.db:
                        self.atlas.db.execute(
                            "DELETE FROM atlas_build_sources WHERE job=?", (job_id,)
                        )
                if not pending:
                    eligible.append(capture)
            except (OSError, ValueError, AtlasError, sqlite3.Error):
                # Keep the durable pending receipt. A subsequent retry or worker
                # acknowledgement can finish without restoring access or metadata.
                pass
            finally:
                with self.atlas.lock, self.atlas.db:
                    self.atlas.db.execute(
                        "UPDATE atlas_removals SET data=json_set(data,'$.last_attempt',?) "
                        "WHERE capture=?",
                        (self.atlas.clock(), capture),
                    )
        if eligible:
            with self.atlas.lock:
                try:
                    # Secure-delete clears freed database cells. Do not report
                    # completion while older sensitive WAL pages remain pinned.
                    timeout = self.atlas.db.execute("PRAGMA busy_timeout").fetchone()[0]
                    try:
                        self.atlas.db.execute("PRAGMA busy_timeout=0")
                        busy, _, _ = self.atlas.db.execute(
                            "PRAGMA wal_checkpoint(TRUNCATE)"
                        ).fetchone()
                    finally:
                        self.atlas.db.execute(f"PRAGMA busy_timeout={timeout}")
                    if busy:
                        return
                    with self.atlas.db:
                        for capture in eligible:
                            self.atlas.db.execute(
                                "UPDATE atlas_removals SET data=json_set(data,"
                                "'$.state','local_removed','$.completed_at',?,"
                                "'$.assets',json('[]'),'$.jobs',json('[]'),'$.analysis_id',null) "
                                "WHERE capture=? AND NOT EXISTS "
                                "(SELECT 1 FROM atlas_source_operations o "
                                "WHERE o.space=atlas_removals.space AND "
                                "(o.capture=atlas_removals.capture OR o.capture IS NULL))",
                                (self.atlas.clock(), capture),
                            )
                except sqlite3.Error:
                    return


def install_removal_routes(app, authorize, resolve, store):
    from relay.atlas_routes import read_json

    base = "/api/sessions/{session}/atlas/spaces/{identifier}/captures/{capture_id}/removal"

    def actor(session, identifier, authorization):
        _, account = resolve(session, identifier, authorization)
        try:
            authorize(authorization)
            return account, True
        except HTTPException:
            return account, False

    @app.get("/api/sessions/{session}/atlas/spaces/{identifier}/removals")
    @app.get("/api/sessions/{session}/atlas/spaces/{identifier}/removals/{before}")
    def receipts(
        session: str,
        identifier: str,
        background: BackgroundTasks,
        before: int = 1001,
        authorization: str | None = Header(default=None),
    ):
        if not 1 <= before <= 1001:
            raise HTTPException(422, "Choose an existing receipt page.")
        account, operator = actor(session, identifier, authorization)
        result = store().removal.receipts(identifier, account, operator, before)
        if result["pending"]:
            background.add_task(store().removal.cleanup)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get(base)
    def preview(
        session: str,
        identifier: str,
        capture_id: str,
        background: BackgroundTasks,
        authorization: str | None = Header(default=None),
    ):
        account, operator = actor(session, identifier, authorization)
        result = store().removal.preview(identifier, capture_id, account, operator)
        if result["state"] == "cleanup_pending":
            background.add_task(store().removal.cleanup)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post(base)
    async def remove(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        background: BackgroundTasks,
        authorization: str | None = Header(default=None),
    ):
        account, operator = actor(session, identifier, authorization)
        value = await read_json(request, RemoveCapture)
        result = store().removal.remove(
            identifier, capture_id, value.confirmation, account, operator
        )
        background.add_task(store().removal.cleanup)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
