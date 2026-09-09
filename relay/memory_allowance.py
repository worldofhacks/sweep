"""Durable provider-stage request allowances, not prices or a billing ledger."""

import hashlib
import os

from relay.atlas import AtlasError

DAY_MS = 86_400_000
SETTINGS = {
    "space": "SWEEP_MEMORY_PROVIDER_CALLS_PER_SPACE_DAY",
    "relay": "SWEEP_MEMORY_PROVIDER_CALLS_PER_RELAY_DAY",
}


def limits():
    result = {}
    for scope, name in SETTINGS.items():
        raw = os.getenv(name, "0")
        # An invalid deployment setting must never accidentally become unlimited.
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 7:
            raise AtlasError("Memory provider request allowances are not validly configured.", 503)
        value = int(raw)
        if value > 1_000_000:
            raise AtlasError("Memory provider request allowances are not validly configured.", 503)
        result[scope] = value
    return result


class MemoryAllowance:
    def __init__(self, memory):
        self.memory = memory
        self.atlas = memory.atlas
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.executescript("""
              CREATE TABLE IF NOT EXISTS memory_provider_counts (
                day INTEGER NOT NULL, scope TEXT NOT NULL, reserved INTEGER NOT NULL,
                PRIMARY KEY(day,scope));
              CREATE TABLE IF NOT EXISTS memory_provider_claims (
                job TEXT NOT NULL, stage TEXT NOT NULL, PRIMARY KEY(job,stage));
            """)

    @staticmethod
    def space_scope(space):
        return "space:" + hashlib.sha256(space.encode()).hexdigest()

    def status(self, space):
        with self.atlas.lock:
            try:
                configured = limits()
                error = None
            except AtlasError as failure:
                configured, error = {"space": 0, "relay": 0}, failure.detail
            day = self.atlas.clock() // DAY_MS
            latest = self.atlas.db.execute(
                "SELECT max(day) FROM memory_provider_counts"
            ).fetchone()[0]
            if latest is not None and day < latest:
                error = "The relay clock moved backwards. Provider requests are paused."
            scopes = {"space": self.space_scope(space), "relay": "relay"}
            used = {}
            for label, scope in scopes.items():
                row = self.atlas.db.execute(
                    "SELECT reserved FROM memory_provider_counts WHERE day=? AND scope=?",
                    (day, scope),
                ).fetchone()
                used[label] = row[0] if row else 0
            return {
                "unit": "provider_stage_reservation",
                "limits": configured,
                "reserved": used,
                "remaining": 0 if error else max(0, min(configured[k] - used[k] for k in scopes)),
                "resets_at": (day + 1) * DAY_MS,
                "error": error,
            }

    def claim(self, capture, job, stage):
        """Commit once immediately before a provider stage; never refund unknown usage.

        Callers must not retry a provider after a timeout. Even a crash between this
        commit and sending consumes allowance. Each stage of the same active job
        can be claimed only once, independently of HTTP request-id support.
        """
        if stage not in {"weather", "transcript", "suggestion"}:
            raise AtlasError("Unknown memory provider stage.", 422)
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            self.memory.require_analysis(capture, job)
            row = self.atlas.db.execute(
                "SELECT space FROM captures WHERE id=?", (capture,)
            ).fetchone()
            space = row[0]
            current = self.memory.get(space, capture)["analysis"]
            if not current["inputs"].get("weather" if stage == "weather" else "ai"):
                raise AtlasError("This provider stage was not requested for this memory.", 403)
            if self.atlas.db.execute(
                "SELECT 1 FROM memory_provider_claims WHERE job=? AND stage=?", (job, stage)
            ).fetchone():
                raise AtlasError(
                    "This provider stage was already reserved. It will not be sent again.", 409
                )
            configured = limits()
            if not all(configured.values()):
                raise AtlasError(
                    "External memory analysis needs explicit Space and relay daily request "
                    "allowances. Local context and originals remain available.",
                    503,
                )
            day = self.atlas.clock() // DAY_MS
            latest = self.atlas.db.execute(
                "SELECT max(day) FROM memory_provider_counts"
            ).fetchone()[0]
            if latest is not None and day < latest:
                raise AtlasError(
                    "The relay clock moved backwards. Provider requests are paused.", 503
                )
            for label, scope in (("space", self.space_scope(space)), ("relay", "relay")):
                row = self.atlas.db.execute(
                    "SELECT reserved FROM memory_provider_counts WHERE day=? AND scope=?",
                    (day, scope),
                ).fetchone()
                if row and row[0] >= configured[label]:
                    raise AtlasError(
                        f"The {label} daily memory provider request allowance is used. "
                        "Local context and originals remain available. "
                        "Try after the UTC day resets.",
                        429,
                    )
                self.atlas.db.execute(
                    "INSERT INTO memory_provider_counts VALUES (?,?,1) "
                    "ON CONFLICT(day,scope) DO UPDATE SET reserved=reserved+1",
                    (day, scope),
                )
            self.atlas.db.execute("INSERT INTO memory_provider_claims VALUES (?,?)", (job, stage))
            # Only aggregate counters expire. Active/crashed-job claims never expire.
            self.atlas.db.execute("DELETE FROM memory_provider_counts WHERE day<?", (day - 30,))
