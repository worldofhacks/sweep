"""Bounded, source-linked calendar projection; never a robot or evidence clock."""

import calendar
import json
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from relay.atlas import AtlasError, AtlasModel


class CaptureDate(AtlasModel):
    precision: Literal["instant", "day", "month", "year", "range", "unknown"]
    value: str | None = Field(default=None, max_length=40)
    end: str | None = Field(default=None, max_length=10)
    note: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def valid_date(self):
        if self.precision == "unknown":
            if self.value or self.end:
                raise ValueError("An unknown date cannot contain a timestamp.")
            self.value, self.end = None, None
            return self
        if not self.value or (self.precision != "range" and self.end is not None):
            raise ValueError("Choose a date with the stated precision.")
        if self.precision == "instant":
            instant = datetime.fromisoformat(self.value.replace("Z", "+00:00"))
            if instant.tzinfo is None or instant.utcoffset().total_seconds() % 60:
                raise ValueError(
                    "An exact time needs an explicit UTC offset, including during DST."
                )
            if instant > datetime.now(UTC):
                raise ValueError("A memory cannot be in the future.")
            self.value = instant.isoformat()
        start, _ = span(self.precision, self.value, self.end)
        if self.precision != "instant" and start > datetime.now(UTC).date().isoformat():
            raise ValueError("Choose a past or current calendar date.")
        return self


class SaveCaptureDate(AtlasModel):
    revision: int = Field(ge=0)
    assertion: CaptureDate


def span(precision, value, end=None):
    if precision == "unknown":
        return None, None
    if precision == "instant":
        start = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return start.isoformat(), start.isoformat()
    if precision == "year":
        if len(value) != 4 or not value.isascii() or not value.isdigit():
            raise ValueError("Use YYYY for a year.")
        return date(int(value), 1, 1).isoformat(), date(int(value), 12, 31).isoformat()
    if precision == "month":
        if len(value) != 7 or value[4] != "-":
            raise ValueError("Use YYYY-MM for a month.")
        start = date.fromisoformat(value + "-01")
        return start.isoformat(), start.replace(
            day=calendar.monthrange(start.year, start.month)[1]
        ).isoformat()
    if len(value) != 10:
        raise ValueError("Use YYYY-MM-DD for a calendar date.")
    start = date.fromisoformat(value)
    finish = date.fromisoformat(end) if precision == "range" and end and len(end) == 10 else start
    if precision == "range" and (not end or len(end) != 10 or finish < start):
        raise ValueError("Choose an end date on or after the start.")
    if precision == "range" and finish > datetime.now(UTC).date():
        raise ValueError("A date range cannot end in the future.")
    return start.isoformat(), finish.isoformat()


def time_value(precision, value, source, end=None, note=""):
    start, finish = span(precision, value, end)
    return {
        "precision": precision,
        "value": value,
        "end": end,
        "source": source,
        "start_date": start,
        "end_date": finish,
        "note": note,
        "utc_offset": datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()[-6:]
        if precision == "instant"
        else None,
    }


def project_time(capture, context, assertion=None):
    """Keep source clocks separate. Unknown-zone EXIF is a calendar date, not UTC."""
    evidence, candidates, warnings = [], [], []
    notes = context.get("notes") or {}
    inspection = (
        context.get("inspection") or (context.get("analysis") or {}).get("inspection") or {}
    )
    for source, raw in (
        ("memory_note", notes.get("occurred_at")),
        ("capture", capture.get("captured_at")),
        ("metadata", inspection.get("timestamp")),
        ("metadata_local", inspection.get("local_timestamp")),
    ):
        if raw is None:
            continue
        evidence.append({"source": source, "value": raw})
        try:
            instant = (
                datetime.fromtimestamp(raw / 1000, UTC)
                if source == "capture"
                else datetime.fromisoformat(raw.replace("Z", "+00:00"))
            )
            if source == "metadata_local" or instant.tzinfo is None:
                candidates.append(
                    time_value(
                        "day",
                        instant.date().isoformat(),
                        "metadata_local",
                        note="Metadata calendar date; time zone unknown.",
                    )
                )
            else:
                candidates.append(time_value("instant", instant.isoformat(), source))
                if instant > datetime.now(UTC):
                    warnings.append("A source clock is ahead of the current time; verify its date.")
        except (ValueError, TypeError, OverflowError, OSError, AttributeError):
            warnings.append("A source date could not be interpreted and was not guessed.")
    if assertion is not None:
        selected = time_value(
            assertion["precision"],
            assertion.get("value"),
            "declared",
            assertion.get("end"),
            assertion.get("note", ""),
        )
    else:
        selected = candidates[0] if candidates else time_value("unknown", None, "unknown")
    return selected, evidence, warnings


class AtlasTimeline:
    def __init__(self, atlas):
        self.atlas = atlas
        with atlas.lock:
            atlas.db.executescript("""
                CREATE TABLE IF NOT EXISTS atlas_capture_dates (
                  space TEXT NOT NULL, capture TEXT NOT NULL, revision INTEGER NOT NULL,
                  data TEXT NOT NULL, PRIMARY KEY(space,capture,revision));
            """)

    def can_edit(self, space, capture, account_id, operator=False):
        return self.atlas.memories.can_edit(space, capture, account_id, operator)

    def entries(self, space, account_id=None, operator=False):
        with self.atlas.lock:
            rows = self.atlas.db.execute(
                "SELECT c.data,m.data,d.revision,d.data FROM captures c "
                "LEFT JOIN memory_contexts m ON m.space=c.space AND m.capture=c.id "
                "LEFT JOIN atlas_capture_dates d ON d.space=c.space AND d.capture=c.id "
                "AND d.revision=(SELECT max(r.revision) FROM atlas_capture_dates r "
                "WHERE r.space=c.space AND r.capture=c.id) WHERE c.space=? LIMIT 500",
                (space,),
            ).fetchall()
            entries = []
            for raw, memory, revision, correction in rows:
                capture, context = json.loads(raw), json.loads(memory) if memory else {}
                current = json.loads(correction) if correction else None
                selected, evidence, warnings = project_time(
                    capture, context, current["assertion"] if current else None
                )
                entries.append(
                    {
                        "capture": capture,
                        "time": selected,
                        "evidence": evidence,
                        "warnings": warnings,
                        "revision": revision or 0,
                        "can_edit": self.can_edit(space, capture, account_id, operator),
                    }
                )

            # Calendar chapters use the supplied offset's date; device epoch dates use UTC.
            # Approximate dates sort by their lower bound, never masquerading as instants.
            def order(entry):
                time = entry["time"]
                instant = (
                    datetime.fromisoformat(time["value"]).timestamp()
                    if time["precision"] == "instant"
                    else float("-inf")
                )
                return time["start_date"], instant, entry["capture"]["id"]

            known = sorted(
                (e for e in entries if e["time"]["start_date"] is not None), key=order, reverse=True
            )
            unknown = sorted(
                (e for e in entries if e["time"]["start_date"] is None),
                key=lambda e: (e["capture"]["uploaded_at"], e["capture"]["id"]),
                reverse=True,
            )
            return {
                "entries": known + unknown,
                "total": len(entries),
                "ordering": (
                    "Calendar date, newest first; exact times within a day; approximate ranges "
                    "by start; stable capture-ID ties. Unknown dates last, by upload time."
                ),
            }

    def history(self, space, capture):
        with self.atlas.lock:
            self.atlas.memories.capture(space, capture)
            return [
                json.loads(row[0])
                for row in self.atlas.db.execute(
                    "SELECT data FROM atlas_capture_dates WHERE space=? AND capture=? "
                    "ORDER BY revision DESC",
                    (space, capture),
                )
            ]

    def save(self, space, capture_id, request, account_id=None, operator=False):
        with self.atlas.lock, self.atlas.db:
            self.atlas.db.execute("BEGIN IMMEDIATE")
            capture = self.atlas.memories.capture(space, capture_id)
            if not self.can_edit(space, capture, account_id, operator):
                raise AtlasError(
                    "Only the Space owner or this capture's account contributor "
                    "can correct its date.",
                    403,
                )
            history = self.history(space, capture_id)
            revision = history[0]["revision"] if history else 0
            if request.revision != revision:
                raise AtlasError(
                    "This date changed in another window. Reload before correcting it.", 409
                )
            if revision >= 100:
                raise AtlasError("This capture has reached its 100-date-correction limit.", 429)
            context = self.atlas.memories.get(space, capture_id)
            before, evidence, _ = project_time(
                capture, context, history[0]["assertion"] if history else None
            )
            value = {
                "revision": revision + 1,
                "assertion": request.assertion.model_dump(),
                "previous": before,
                "evidence": evidence,
                "actor": account_id if not operator else "workspace-operator",
                "changed_at": self.atlas.clock(),
            }
            self.atlas.db.execute(
                "INSERT INTO atlas_capture_dates VALUES (?,?,?,?)",
                (space, capture_id, revision + 1, json.dumps(value)),
            )
            return value


def install_timeline_routes(app, authorize, resolve, store):
    from relay.atlas_routes import read_json

    def actor(session, identifier, authorization):
        _, account_id = resolve(session, identifier, authorization)
        try:
            authorize(authorization)
            return account_id, True
        except HTTPException:
            return account_id, False

    def response(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    base = "/api/sessions/{session}/atlas/spaces/{identifier}"

    @app.get(base + "/timeline")
    def timeline(session: str, identifier: str, authorization: str | None = Header(default=None)):
        account_id, operator = actor(session, identifier, authorization)
        return response(store().timeline.entries(identifier, account_id, operator))

    @app.get(base + "/captures/{capture_id}/date")
    def history(
        session: str,
        identifier: str,
        capture_id: str,
        authorization: str | None = Header(default=None),
    ):
        actor(session, identifier, authorization)
        return response({"history": store().timeline.history(identifier, capture_id)})

    @app.post(base + "/captures/{capture_id}/date")
    async def save(
        session: str,
        identifier: str,
        capture_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        account_id, operator = actor(session, identifier, authorization)
        value = await read_json(request, SaveCaptureDate)
        return response(store().timeline.save(identifier, capture_id, value, account_id, operator))
