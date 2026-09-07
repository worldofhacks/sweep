"""Durable session-scoped, immutable map revisions and audited approval.

The transport supplies an authenticated actor. No document field grants approval,
and this service contains no fixture map or hardware-evidence substitutes.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tools.world_bundle import (
    VALIDATOR_VERSION,
    BundleError,
    build_world_bundle,
    canonical_json,
    content_hash,
    validate_draft,
    validate_world_bundle,
)

MAX_SESSION_BYTES = 256 * 1024 * 1024
MAX_SESSION_REVISIONS = 1024


class MapAuthoringError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 409) -> None:
        super().__init__(detail)
        self.code, self.detail, self.status_code = code, detail, status_code


def _identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 256
        or value != value.strip()
        or not value.isprintable()
    ):
        raise MapAuthoringError("invalid_request", f"{name} must be bounded normalized text", 400)
    return value


def _session(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 512
        or value != value.strip()
        or not value.isprintable()
    ):
        raise MapAuthoringError("invalid_request", "invalid relay session identifier", 400)
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise MapAuthoringError("invalid_request", "invalid session encoding", 400) from error
    return value


def _reference(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"bundleId", "revision", "contentHash"}:
        raise MapAuthoringError("invalid_request", "invalid map revision reference", 400)
    bundle = _identifier(value["bundleId"], "bundle id")
    revision = _identifier(value["revision"], "revision")
    digest = value["contentHash"]
    if (
        not revision.isascii()
        or not revision.isdigit()
        or revision.startswith("0")
        or len(revision) > 15
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise MapAuthoringError("invalid_request", "invalid revision number or SHA-256", 400)
    return {"bundleId": bundle, "revision": revision, "contentHash": digest}


class MapAuthoringStore:
    def __init__(
        self, database_path: str | Path, *, clock_ms: Callable[[], int] | None = None
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS map_heads (
                    session TEXT NOT NULL, bundle_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY (session, bundle_id)
                );
                CREATE TABLE IF NOT EXISTS map_revisions (
                    session TEXT NOT NULL, bundle_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, draft TEXT NOT NULL, bundle TEXT,
                    map_version TEXT, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY (session, bundle_id, revision)
                );
                CREATE TABLE IF NOT EXISTS map_validations (
                    validation_id TEXT PRIMARY KEY, session TEXT NOT NULL, bundle_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, content_hash TEXT NOT NULL,
                    validator_version TEXT NOT NULL, valid INTEGER NOT NULL, issues TEXT NOT NULL,
                    created_by TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS map_approvals (
                    audit_id TEXT PRIMARY KEY, validation_id TEXT NOT NULL,
                    session TEXT NOT NULL, bundle_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, map_version TEXT NOT NULL,
                    approved_by TEXT NOT NULL, approved_at INTEGER NOT NULL,
                    UNIQUE (session, bundle_id, revision)
                );
                CREATE TABLE IF NOT EXISTS map_audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                    action TEXT NOT NULL, actor TEXT NOT NULL, at_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS map_revision_session ON map_revisions(session);
                CREATE INDEX IF NOT EXISTS map_approval_session
                    ON map_approvals(session, map_version);
            """)
            for table in ("map_revisions", "map_validations", "map_approvals", "map_audit"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'map evidence is immutable'); END"
                    )

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            if connection.in_transaction:
                connection.commit()
        except sqlite3.Error as error:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise MapAuthoringError("storage_error", "map storage operation failed", 503) from error
        except BaseException:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _now(self) -> int:
        now = self.clock_ms()
        if type(now) is not int or not 0 <= now <= 2**53 - 1:
            raise MapAuthoringError("clock_unavailable", "map service clock is invalid", 503)
        return now

    def _draft_issues(self, draft: dict[str, Any]) -> list[dict[str, str]]:
        issues = validate_draft(draft)
        metadata = draft.get("metadata")
        created_at = metadata.get("createdAt") if isinstance(metadata, dict) else None
        if type(created_at) is int and created_at > self._now():
            issues.append(
                {"path": "metadata.createdAt", "message": "creation time is in the future"}
            )
        return issues[:256]

    @staticmethod
    def _row_reference(row: sqlite3.Row) -> dict[str, str]:
        return {
            "bundleId": row["bundle_id"],
            "revision": str(row["revision"]),
            "contentHash": row["content_hash"],
        }

    def _read(
        self, connection: sqlite3.Connection, session: str, reference: dict[str, str]
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM map_revisions WHERE session=? AND bundle_id=? AND revision=?",
            (session, reference["bundleId"], int(reference["revision"])),
        ).fetchone()
        if row is None:
            raise MapAuthoringError(
                "revision_not_found", "map revision is not in this session", 404
            )
        if row["content_hash"] != reference["contentHash"]:
            raise MapAuthoringError("revision_mismatch", "map revision hash does not match")
        try:
            draft = json.loads(row["draft"])
            if canonical_json(draft) != row["draft"] or content_hash(draft) != row["content_hash"]:
                raise BundleError("stored draft hash mismatch")
            if row["bundle"] is not None:
                bundle = json.loads(row["bundle"])
                if (
                    validate_world_bundle(bundle)
                    or canonical_json(build_world_bundle(draft)) != row["bundle"]
                ):
                    raise BundleError("stored bundle does not match the draft")
        except (ValueError, TypeError, KeyError) as error:
            raise MapAuthoringError(
                "integrity_error", "stored map integrity check failed", 503
            ) from error
        return row

    @staticmethod
    def _is_head(connection: sqlite3.Connection, session: str, reference: dict[str, str]) -> bool:
        row = connection.execute(
            "SELECT revision FROM map_heads WHERE session=? AND bundle_id=?",
            (session, reference["bundleId"]),
        ).fetchone()
        return row is not None and row["revision"] == int(reference["revision"])

    def _audit(
        self, connection: sqlite3.Connection, session: str, action: str, actor: str, payload: object
    ) -> None:
        connection.execute(
            "INSERT INTO map_audit(session,action,actor,at_ms,payload) VALUES(?,?,?,?,?)",
            (session, action, actor, self._now(), canonical_json(payload)),
        )

    def list(self, session: str) -> list[dict[str, str]]:
        session = _session(session)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM map_revisions WHERE session=? ORDER BY rowid DESC LIMIT 256",
                (session,),
            ).fetchall()
            return [
                {
                    **self._row_reference(row),
                    "label": (
                        f"{row['map_version'] or 'Incomplete map'} · revision {row['revision']}"
                    )[:256],
                }
                for row in rows
            ]

    def load(self, session: str, reference: object) -> dict[str, Any]:
        session, ref = _session(session), _reference(reference)
        with self._connection() as connection:
            row = self._read(connection, session, ref)
            return {"reference": ref, "draft": json.loads(row["draft"])}

    def save(
        self, session: str, draft: object, expected_revision: object | None, actor: str
    ) -> dict[str, str]:
        session, actor = _session(session), _identifier(actor, "authenticated actor")
        if not isinstance(draft, dict):
            raise MapAuthoringError("invalid_request", "map draft must be an object", 400)
        try:
            encoded = canonical_json(draft)
            detached = json.loads(encoded)
        except BundleError as error:
            raise MapAuthoringError("invalid_request", str(error), 400) from error
        digest = content_hash(detached)
        ref = None if expected_revision is None else _reference(expected_revision)
        issues = self._draft_issues(detached)
        bundle = None if issues else canonical_json(build_world_bundle(detached))
        metadata = detached.get("metadata")
        map_version = metadata.get("mapVersion") if isinstance(metadata, dict) else None
        if not isinstance(map_version, str) or len(map_version) > 256:
            map_version = None
        with self._connection(write=True) as connection:
            quota = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(length(CAST(draft AS BLOB))"
                "+COALESCE(length(CAST(bundle AS BLOB)),0)),0) AS bytes "
                "FROM map_revisions WHERE session=?",
                (session,),
            ).fetchone()
            if (
                quota["count"] >= MAX_SESSION_REVISIONS
                or quota["bytes"] + len(encoded.encode()) + len((bundle or "").encode())
                > MAX_SESSION_BYTES
            ):
                raise MapAuthoringError("storage_quota", "session map storage quota reached", 413)
            if ref is None:
                bundle_id, revision = f"map-{uuid.uuid4().hex}", 1
                connection.execute(
                    "INSERT INTO map_heads VALUES(?,?,?)", (session, bundle_id, revision)
                )
            else:
                self._read(connection, session, ref)
                if not self._is_head(connection, session, ref):
                    raise MapAuthoringError(
                        "revision_conflict", "map changed; load the current revision"
                    )
                bundle_id, revision = ref["bundleId"], int(ref["revision"]) + 1
                connection.execute(
                    "UPDATE map_heads SET revision=? WHERE session=? AND bundle_id=?",
                    (revision, session, bundle_id),
                )
            reference = {"bundleId": bundle_id, "revision": str(revision), "contentHash": digest}
            connection.execute(
                "INSERT INTO map_revisions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    session,
                    bundle_id,
                    revision,
                    digest,
                    encoded,
                    bundle,
                    map_version,
                    actor,
                    self._now(),
                ),
            )
            receipt = self._validation(connection, session, reference, actor, issues)
            self._audit(
                connection,
                session,
                "map_saved",
                actor,
                {"reference": reference, "validation": receipt},
            )
            return reference

    def _validation(
        self,
        connection: sqlite3.Connection,
        session: str,
        reference: dict[str, str],
        actor: str,
        issues: list[dict[str, str]],
    ) -> dict[str, Any]:
        encoded_issues = canonical_json(issues)
        existing = connection.execute(
            "SELECT * FROM map_validations WHERE session=? AND bundle_id=? AND revision=? "
            "AND content_hash=? AND validator_version=? AND valid=? AND issues=? "
            "ORDER BY rowid DESC LIMIT 1",
            (
                session,
                reference["bundleId"],
                int(reference["revision"]),
                reference["contentHash"],
                VALIDATOR_VERSION,
                int(not issues),
                encoded_issues,
            ),
        ).fetchone()
        if existing is not None:
            return {
                "reference": reference,
                "validationId": existing["validation_id"],
                "valid": not issues,
                "issues": issues,
            }
        validation_id = f"validation-{uuid.uuid4().hex}"
        connection.execute(
            "INSERT INTO map_validations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                validation_id,
                session,
                reference["bundleId"],
                int(reference["revision"]),
                reference["contentHash"],
                VALIDATOR_VERSION,
                int(not issues),
                encoded_issues,
                actor,
                self._now(),
            ),
        )
        return {
            "reference": reference,
            "validationId": validation_id,
            "valid": not issues,
            "issues": issues,
        }

    def validate(self, session: str, reference: object, actor: str) -> dict[str, Any]:
        session, actor = _session(session), _identifier(actor, "authenticated actor")
        ref = _reference(reference)
        with self._connection(write=True) as connection:
            row = self._read(connection, session, ref)
            receipt = self._validation(
                connection, session, ref, actor, self._draft_issues(json.loads(row["draft"]))
            )
            self._audit(connection, session, "map_validated", actor, receipt)
            return receipt

    @staticmethod
    def _approval_receipt(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reference": MapAuthoringStore._row_reference(row),
            "validationId": row["validation_id"],
            "auditId": row["audit_id"],
            "approvedBy": row["approved_by"],
            "approvedAt": row["approved_at"],
        }

    def approve(
        self, session: str, reference: object, validation_id: str, actor: str
    ) -> dict[str, Any]:
        session, actor = _session(session), _identifier(actor, "authenticated actor")
        ref, validation_id = _reference(reference), _identifier(validation_id, "validation id")
        with self._connection(write=True) as connection:
            row = self._read(connection, session, ref)
            if not self._is_head(connection, session, ref):
                raise MapAuthoringError(
                    "revision_conflict", "only the current saved revision can be approved"
                )
            validation = connection.execute(
                "SELECT * FROM map_validations WHERE validation_id=?", (validation_id,)
            ).fetchone()
            if (
                validation is None
                or validation["session"] != session
                or self._row_reference(validation) != ref
                or validation["validator_version"] != VALIDATOR_VERSION
            ):
                raise MapAuthoringError(
                    "validation_mismatch", "validation is not current for this exact revision"
                )
            if (
                not validation["valid"]
                or json.loads(validation["issues"])
                or self._draft_issues(json.loads(row["draft"]))
                or row["bundle"] is None
            ):
                raise MapAuthoringError(
                    "validation_failed", "invalid map revisions cannot be approved", 422
                )
            previous = connection.execute(
                "SELECT * FROM map_approvals WHERE session=? AND map_version=?",
                (session, row["map_version"]),
            ).fetchall()
            if any(approval["content_hash"] != ref["contentHash"] for approval in previous):
                raise MapAuthoringError(
                    "map_version_reused", "changed approved content requires a new map version"
                )
            existing = next((a for a in previous if self._row_reference(a) == ref), None)
            if existing is not None:
                return self._approval_receipt(existing)
            audit_id = f"approval-{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO map_approvals VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    audit_id,
                    validation_id,
                    session,
                    ref["bundleId"],
                    int(ref["revision"]),
                    ref["contentHash"],
                    row["map_version"],
                    actor,
                    self._now(),
                ),
            )
            approval = connection.execute(
                "SELECT * FROM map_approvals WHERE audit_id=?", (audit_id,)
            ).fetchone()
            receipt = self._approval_receipt(approval)
            self._audit(connection, session, "map_approved", actor, receipt)
            return receipt

    def compare(self, session: str, left: object, right: object) -> dict[str, Any]:
        session = _session(session)
        left_ref, right_ref = _reference(left), _reference(right)
        with self._connection() as connection:
            before = json.loads(self._read(connection, session, left_ref)["draft"])
            after = json.loads(self._read(connection, session, right_ref)["draft"])
        changes: list[dict[str, str]] = []
        missing = object()

        def walk(a: Any, b: Any, path: str) -> None:
            if type(a) is type(b) and not isinstance(a, dict | list) and a == b:
                return
            if len(changes) >= 1024:
                raise MapAuthoringError(
                    "comparison_too_large", "revision comparison exceeds 1024 changes", 413
                )
            if isinstance(a, dict) and isinstance(b, dict):
                for key in sorted(a.keys() | b.keys()):
                    walk(a.get(key, missing), b.get(key, missing), f"{path}.{key}" if path else key)
            elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                for index, (first, second) in enumerate(zip(a, b, strict=True)):
                    walk(first, second, f"{path}.{index}")
            else:

                def display(value: object) -> str:
                    if value is missing:
                        return "<absent>"
                    text = canonical_json(value)
                    if len(text) <= 4096:
                        return text
                    return f"{text[:3900]}… [SHA-256 {content_hash(value)}; {len(text)} characters]"

                changes.append(
                    {"path": path[:512] or "draft", "before": display(a), "after": display(b)}
                )

        walk(before, after, "")
        return {"left": left_ref, "right": right_ref, "changes": changes}

    def approved_bundle(self, session: str, reference: object | None = None) -> dict[str, Any]:
        """Return a current approved static bundle; editing immediately revokes its use.

        The result authorizes no navigation dispatch and contains no derived flight
        clearance unless independently generated, pinned artifacts are added later.
        """
        session = _session(session)
        ref = None if reference is None else _reference(reference)
        with self._connection() as connection:
            if ref is None:
                approvals = connection.execute(
                    "SELECT a.* FROM map_approvals a JOIN map_heads h "
                    "ON a.session=h.session AND a.bundle_id=h.bundle_id AND a.revision=h.revision "
                    "WHERE a.session=? ORDER BY a.rowid DESC LIMIT 2",
                    (session,),
                ).fetchall()
                if len(approvals) > 1:
                    raise MapAuthoringError(
                        "approval_ambiguous", "multiple approved maps require an explicit revision"
                    )
                approval = approvals[0] if approvals else None
            else:
                approval = connection.execute(
                    "SELECT a.* FROM map_approvals a JOIN map_heads h "
                    "ON a.session=h.session AND a.bundle_id=h.bundle_id AND a.revision=h.revision "
                    "WHERE a.session=? AND a.bundle_id=? AND a.revision=? AND a.content_hash=?",
                    (session, ref["bundleId"], int(ref["revision"]), ref["contentHash"]),
                ).fetchone()
            if approval is None:
                raise MapAuthoringError(
                    "approval_unavailable", "no current approved map bundle is available"
                )
            ref = self._row_reference(approval)
            row = self._read(connection, session, ref)
            validation = connection.execute(
                "SELECT * FROM map_validations WHERE validation_id=?", (approval["validation_id"],)
            ).fetchone()
            if (
                row["bundle"] is None
                or validation is None
                or not validation["valid"]
                or validation["session"] != session
                or self._row_reference(validation) != ref
                or validation["validator_version"] != VALIDATOR_VERSION
                or json.loads(validation["issues"])
                or self._draft_issues(json.loads(row["draft"]))
            ):
                raise MapAuthoringError("integrity_error", "approved bundle is unavailable", 503)
            return {
                "reference": ref,
                "approval": self._approval_receipt(approval),
                "bundle": json.loads(row["bundle"]),
            }

    def audit_records(self, session: str) -> list[dict[str, Any]]:
        """Bounded inspection for the host audit/export layer."""
        session = _session(session)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM map_audit WHERE session=? ORDER BY sequence DESC LIMIT 4096",
                (session,),
            ).fetchall()
            return [
                {
                    "sequence": row["sequence"],
                    "action": row["action"],
                    "actor": row["actor"],
                    "at": row["at_ms"],
                    "payload": json.loads(row["payload"]),
                }
                for row in reversed(rows)
            ]
