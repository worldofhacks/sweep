"""Durable named-destination reviews over approved, versioned map evidence.

Production defaults return typed per-node refusals. A flight executor may dispatch
one retained, qualified aircraft route after confirmation rechecks frozen inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from functools import wraps
from pathlib import Path
from typing import Protocol

from relay.platform_identity import platform_device_class

MAX_JSON_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_TARGETS = 64
MAX_DESTINATIONS = 128
MAX_SAFE_INTEGER = 2**53 - 1
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_CLASSES = frozenset({"aircraft", "ground_vehicle"})


class NavigationError(ValueError):
    def __init__(self, code: str, detail: str, status_code: int = 409) -> None:
        self.code, self.detail = code, detail
        self.status_code = status_code
        super().__init__(detail)


def _storage_errors(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except sqlite3.Error as error:
            raise NavigationError(
                "storage_unavailable", "Navigation review storage is unavailable.", 503
            ) from error

    return guarded


class RoutePreviewProvider(Protocol):
    """Trusted class-planner seam. Its output remains explicitly non-dispatchable.

    Returns one route or refusal for every supplied selected target, using the
    exact frontend route/outcome DTOs. Coordinate transforms, obstacle envelopes,
    arrival allocation, and measurements belong to the provider. This service
    will never fabricate them from a zone centroid or generic telemetry x/y.
    """

    def __call__(
        self, request: dict[str, object], catalog: dict[str, object], state: dict[str, object]
    ) -> dict[str, object]: ...


def _fail(code: str, detail: str) -> None:
    raise NavigationError(code, detail)


def _json(
    value: object, maximum: int = MAX_JSON_BYTES, *, max_depth: int = 16, max_items: int = 100_000
) -> str:
    remaining = max_items

    def walk(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > max_depth:
            _fail("invalid_payload", "Navigation JSON exceeds its structural bounds.")
        if item is None or type(item) in {str, bool, int}:
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                walk(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str and len(key) <= 128 for key in item):
            for child in item.values():
                walk(child, depth + 1)
            return
        _fail("invalid_payload", "Navigation input must contain bounded, finite JSON values.")

    try:
        walk(value, 0)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > maximum:
            _fail("invalid_payload", "Navigation JSON exceeds its byte bound.")
        return encoded
    except (RecursionError, OverflowError, UnicodeError, ValueError) as error:
        if isinstance(error, NavigationError):
            raise
        raise NavigationError("invalid_payload", "Navigation input is invalid JSON.") from error


def _copy(value: object, maximum: int = MAX_JSON_BYTES):
    return json.loads(_json(value, maximum))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _exact(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        _fail("invalid_payload", "Navigation fields do not match the contract.")
    return value


def _text(value: object, maximum: int = 128) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or not value.isprintable()
    ):
        _fail("invalid_payload", "Navigation text must be bounded and printable.")
    if len(value.encode("utf-16-le")) // 2 > maximum:
        _fail("invalid_payload", "Navigation text exceeds the shared console string bound.")
    return value


def _identity(value: object) -> str:
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        _fail("invalid_payload", "Navigation identity is invalid.")
    return value


def _integer(value: object, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("invalid_payload", "Navigation integer is outside its bounds.")
    return value


def _targets(raw: object, *, allow_empty: bool = False) -> list[dict]:
    if type(raw) is not list or not (0 if allow_empty else 1) <= len(raw) <= MAX_TARGETS:
        _fail("invalid_selection", "Select between one and 64 current devices.")
    result = []
    for entry in raw:
        item = _exact(entry, {"id", "deviceClass", "epoch"})
        _integer(item["id"], 1, 2**31 - 1)
        _integer(item["epoch"], 1)
        if type(item["deviceClass"]) is not str or item["deviceClass"] not in _CLASSES:
            _fail("invalid_selection", "Selected devices require an explicit supported class.")
        result.append(item)
    if len({item["id"] for item in result}) != len(result):
        _fail("invalid_selection", "Selected device identities must be unique.")
    return _copy(result)


def _normalized(value: str) -> str:
    # Match the console's NFKC + en-US lowercase rule; casefold would silently
    # broaden identities such as Straße to Strasse that its resolver distinguishes.
    return " ".join(unicodedata.normalize("NFKC", value).split()).lower()


def state_projection(raw: Mapping[str, object]) -> dict[str, object]:
    """Project only authoritative relay readiness, class and current-epoch facts.

    Telemetry coordinates are deliberately absent: their presence alone proves
    neither a map registration nor the freshness/qualification of a world pose.
    The material facts exclude event IDs and wall-clock ticks, so a new state
    event by itself does not invalidate a review. Changes to readiness, current
    telemetry/pose evidence, selection, authority or active plans do invalidate it.
    """
    state = _copy(dict(raw))
    session = _text(state.get("session"), 512)
    roster = _integer(state.get("roster_version"))
    drones = state.get("drones")
    if type(drones) is not list or len(drones) > MAX_TARGETS:
        _fail("state_unavailable", "The relay did not supply a bounded device roster.")
    nodes = []
    for raw_node in drones:
        if type(raw_node) is not dict:
            _fail("state_unavailable", "The relay supplied an invalid device record.")
        target = _targets(
            [
                {
                    "id": raw_node.get("drone_id"),
                    "deviceClass": platform_device_class(raw_node),
                    "epoch": raw_node.get("connection_epoch"),
                }
            ]
        )[0]
        telemetry = raw_node.get("telemetry")
        telemetry_current = (
            type(telemetry) is dict
            and telemetry.get("connection_epoch") == target["epoch"]
            and type(telemetry.get("t")) is int
            and type(state.get("t")) is int
            and telemetry["t"] <= state["t"]
        )
        nodes.append(
            {
                "target": target,
                "ready": raw_node.get("membership") == "ready"
                and raw_node.get("readiness_reasons") == []
                and raw_node.get("selectable") is True
                and telemetry_current,
                "authority": raw_node.get("control_authority") is True,
                "motionState": raw_node.get("flight_state"),
                "capabilities": raw_node.get("adapter_capabilities", []),
                "telemetry": telemetry,
                "readinessReasons": raw_node.get("readiness_reasons"),
            }
        )
    if len({node["target"]["id"] for node in nodes}) != len(nodes):
        _fail("state_unavailable", "The relay supplied duplicate device identities.")
    ids = state.get("selection")
    if (
        type(ids) is not list
        or len(ids) > MAX_TARGETS
        or any(type(item) is not int for item in ids)
        or len(set(ids)) != len(ids)
    ):
        _fail("state_unavailable", "The relay selection is invalid.")
    by_id = {node["target"]["id"]: node for node in nodes}
    if any(type(node_id) is not int or node_id not in by_id for node_id in ids):
        _fail("selection_changed", "A selected device is absent from the current roster.")
    return {
        "session": session,
        "rosterVersion": roster,
        "selected": [by_id[node_id]["target"] for node_id in ids],
        "nodes": sorted(nodes, key=lambda node: node["target"]["id"]),
        "enabledIntentNames": state.get("enabled_intent_names", []),
        "estop": state.get("estop") is not False,
        "mode": state.get("mode"),
        "armed": state.get("armed"),
        "pending": state.get("pending"),
        "acceptedPlan": state.get("accepted_plan"),
    }


class FlightNavigationExecution(Protocol):
    def preview(self, session: str, preview: Mapping[str, object]) -> Mapping[str, object]: ...

    def confirm(self, session: str, preview: Mapping[str, object]) -> Mapping[str, object]: ...


class NavigationService:
    @_storage_errors
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock_ms: Callable[[], int],
        approved_bundle: Callable[..., dict[str, object]],
        state: Callable[[str], Mapping[str, object]],
        motion_config: Callable[[str], dict[str, object] | None],
        route_preview: RoutePreviewProvider | None = None,
        flight_execution: FlightNavigationExecution | None = None,
        review_ttl_ms: int = 15_000,
        max_previews: int = 256,
    ) -> None:
        self.clock_ms, self.approved_bundle = clock_ms, approved_bundle
        self.state, self.motion_config = state, motion_config
        self.route_preview, self.flight_execution = route_preview, flight_execution
        self.review_ttl_ms = _integer(review_ttl_ms, 1, 60_000)
        self.max_previews = _integer(max_previews, 1, 4096)
        self._lock = threading.RLock()
        self._boot_id = str(uuid.uuid4())
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("""CREATE TABLE IF NOT EXISTS navigation_previews (
                preview_id TEXT PRIMARY KEY, session TEXT NOT NULL, intent_id TEXT NOT NULL,
                created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                preview_json TEXT NOT NULL, preview_hash TEXT NOT NULL,
                context_hash TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(session, intent_id))""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS navigation_state_generations (
                session TEXT PRIMARY KEY, generation INTEGER NOT NULL, state_hash TEXT NOT NULL)""")
            self._db.execute("""CREATE TRIGGER IF NOT EXISTS navigation_preview_immutable
                BEFORE UPDATE OF session,intent_id,created_at,expires_at,preview_json,
                preview_hash,context_hash ON navigation_previews BEGIN
                SELECT RAISE(ABORT,'navigation preview evidence is immutable'); END""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS navigation_map_selections (
                selection_id TEXT PRIMARY KEY, session TEXT NOT NULL, reference_json TEXT NOT NULL,
                selected_by TEXT NOT NULL, selected_at INTEGER NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS navigation_active_maps (
                session TEXT PRIMARY KEY, selection_id TEXT NOT NULL
                REFERENCES navigation_map_selections(selection_id))""")
            for action in ("UPDATE", "DELETE"):
                self._db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS navigation_selection_immutable_{action.lower()} "
                    f"BEFORE {action} ON navigation_map_selections BEGIN "
                    "SELECT RAISE(ABORT,'navigation map selection audit is immutable'); END"
                )
            self._db.commit()
        except BaseException:
            self._db.close()
            raise

    @_storage_errors
    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _now(self) -> int:
        return _integer(self.clock_ms(), maximum=MAX_SAFE_INTEGER - self.review_ttl_ms)

    @_storage_errors
    def observe_state(self, session: str, raw: Mapping[str, object]) -> None:
        """Retire reviews on every material state transition, including A→B→A.

        Bind this to each published authoritative state event. It is also called
        for direct reads, so requests remain conservative if an event is delayed.
        """
        projected = state_projection(raw)
        if projected["session"] != session:
            _fail("session_changed", "The observed state belongs to another session.")
        digest = _hash(projected)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT generation,state_hash FROM navigation_state_generations WHERE session=?",
                (session,),
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO navigation_state_generations VALUES (?,1,?)", (session, digest)
                )
            elif row[1] != digest:
                self._db.execute(
                    """UPDATE navigation_state_generations
                    SET generation=generation+1,state_hash=? WHERE session=?""",
                    (digest, session),
                )

    @_storage_errors
    def invalidate(self, session: str) -> None:
        """Retire a session's reviews when authoritative external inputs change."""
        _text(session, 512)
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO navigation_state_generations VALUES (?,1,'')
                ON CONFLICT(session) DO UPDATE SET generation=generation+1""",
                (session,),
            )

    @_storage_errors
    def select_map(self, session: str, raw: object, actor: str = "console") -> dict[str, object]:
        """Select one exact current approved revision, independently of approval.

        The actor is supplied by the authenticated host route. A selection record
        carries no motion permission. It is durable and cannot follow a changed
        map head automatically, even within the same bundle.
        """
        _text(session, 512)
        _text(actor, 256)
        request = _exact(_copy(raw), {"reference"})
        reference = _exact(request["reference"], {"bundleId", "revision", "contentHash"})
        _identity(reference["bundleId"])
        _text(reference["revision"])
        if (
            type(reference["contentHash"]) is not str
            or _HASH.fullmatch(reference["contentHash"]) is None
        ):
            _fail("invalid_payload", "The selected map requires its exact revision content hash.")
        with self._lock:
            try:
                approved = self.approved_bundle(session, _copy(reference))
            except ValueError as error:
                raise NavigationError(
                    "map_unavailable", "Select a current approved map revision in this session."
                ) from error
            if type(approved) is not dict or approved.get("reference") != reference:
                _fail("map_unavailable", "The selected revision has no matching current approval.")
            receipt = {
                "reference": reference,
                "selectionId": str(uuid.uuid4()),
                "selectedBy": actor,
                "selectedAt": self._now(),
            }
            with self._db:
                count = self._db.execute(
                    "SELECT COUNT(*) FROM navigation_map_selections WHERE session=?", (session,)
                ).fetchone()[0]
                if count >= 4096:
                    _fail("selection_capacity", "This session's map selection audit is full.")
                self._db.execute(
                    "INSERT INTO navigation_map_selections VALUES (?,?,?,?,?)",
                    (
                        receipt["selectionId"],
                        session,
                        _json(reference),
                        actor,
                        receipt["selectedAt"],
                    ),
                )
                self._db.execute(
                    """INSERT INTO navigation_active_maps VALUES (?,?)
                    ON CONFLICT(session) DO UPDATE SET selection_id=excluded.selection_id""",
                    (session, receipt["selectionId"]),
                )
                self._db.execute(
                    """INSERT INTO navigation_state_generations VALUES (?,1,'')
                    ON CONFLICT(session) DO UPDATE SET generation=generation+1""",
                    (session,),
                )
            return _copy(receipt)

    def _current_approved(self, session: str) -> dict[str, object]:
        row = self._db.execute(
            """SELECT a.session,s.session,s.reference_json
            FROM navigation_active_maps a LEFT JOIN navigation_map_selections s
            ON a.selection_id=s.selection_id WHERE a.session=?""",
            (session,),
        ).fetchone()
        if row is None:
            return self.approved_bundle(session)
        try:
            reference = json.loads(row[2])
            if row[0] != row[1] or _json(reference) != row[2]:
                raise ValueError("stored map selection mismatch")
            _exact(reference, {"bundleId", "revision", "contentHash"})
        except (ValueError, TypeError) as error:
            raise NavigationError(
                "storage_unavailable", "The retained active map selection is invalid.", 503
            ) from error
        return self.approved_bundle(session, reference)

    @_storage_errors
    def current_approved_bundle(self, session: str) -> dict[str, object]:
        """Share the same exact active map with verified position consumers."""
        _text(session, 512)
        with self._lock:
            return self._current_approved(session)

    def _catalog(self, session: str, now: int) -> dict[str, object]:
        _text(session, 512)
        try:
            approved = self._current_approved(session)
        except NavigationError:
            raise
        except ValueError as error:
            raise NavigationError(
                "map_unavailable", "No current approved world-map bundle is available."
            ) from error
        if type(approved) is not dict:
            _fail("map_unavailable", "No current approved world-map bundle is available.")
        bundle = approved.get("bundle")
        reference, approval = approved.get("reference"), approved.get("approval")
        if type(bundle) is not dict or type(reference) is not dict or type(approval) is not dict:
            _fail("map_unavailable", "The approved map reference is unavailable.")
        manifest = bundle.get("manifest")
        if type(manifest) is not dict or manifest.get("frame") != "world":
            _fail("map_frame_unavailable", "The approved map must have a verified world frame.")
        floor_id, map_id = _identity(manifest.get("floorId")), _identity(reference.get("bundleId"))
        version = _identity(manifest.get("mapVersion"))
        content_hash = reference.get("contentHash")
        if type(content_hash) is not str or _HASH.fullmatch(content_hash) is None:
            _fail("map_unavailable", "The approved map content hash is invalid.")
        if approval.get("reference") != reference:
            _fail("map_unavailable", "The approval does not bind the current map revision.")
        approval_id = _identity(approval.get("auditId"))
        configuration = self.motion_config(session)
        if type(configuration) is not dict or not configuration or len(configuration) > 64:
            _fail(
                "motion_config_unavailable",
                "Authoritative measured motion configuration is absent.",
            )
        configuration = json.loads(
            _json(configuration, MAX_CONFIG_BYTES, max_depth=6, max_items=2048)
        )
        zones, excluded = bundle.get("zones"), bundle.get("obstacles", [])
        if (
            type(zones) is not list
            or type(excluded) is not list
            or len(zones) + len(excluded) > MAX_DESTINATIONS
        ):
            _fail("map_unavailable", "The approved map has an invalid destination catalog.")
        destinations = []
        for zone, is_excluded in [(zone, False) for zone in zones] + [
            (zone, True) for zone in excluded
        ]:
            if type(zone) is not dict:
                _fail("map_unavailable", "The approved map contains an invalid zone.")
            aliases = zone.get("aliases", [])
            if type(aliases) is not list or len(aliases) > 16:
                _fail("map_unavailable", "Destination aliases exceed the contract bounds.")
            aliases = [_text(alias) for alias in aliases]
            if len({_normalized(alias) for alias in aliases}) != len(aliases):
                _fail("map_unavailable", "Destination aliases contain duplicates.")
            destinations.append(
                {
                    "zoneId": _identity(zone.get("id")),
                    "name": _text(zone.get("name")),
                    "aliases": aliases,
                    "floorId": floor_id,
                    "excluded": is_excluded,
                    # A reviewed polygon is not evidence of a class-qualified route.
                    "reachability": "unknown",
                    "allowedClasses": sorted(_CLASSES),
                }
            )
        if len({zone["zoneId"] for zone in destinations}) != len(destinations):
            _fail("map_unavailable", "The approved map contains duplicate zone identities.")
        # These pins identify actual static authoring documents. They are never
        # substituted for a generated #82 flight grid or a qualified route artifact.
        geometry = {key: bundle.get(key) for key in ("zones", "corridors", "obstacles", "geofence")}
        map_ref = {
            "mapId": map_id,
            "floorId": floor_id,
            "frame": "world",
            "mapPin": {"version": version, "contentSha256": content_hash},
            "geometryPin": {
                "version": f"static-{_hash(geometry)[:16]}",
                "contentSha256": _hash(geometry),
            },
            "navigationPin": {
                "version": f"catalog-{_hash(destinations)[:16]}",
                "contentSha256": _hash(destinations),
            },
            "accepted": True,
            "approvalId": approval_id,
        }
        catalog_identity = {"map": map_ref, "destinations": destinations}
        return {
            "session": session,
            "catalogVersion": _hash(catalog_identity),
            "receivedAt": now,
            "expiresAt": now + self.review_ttl_ms,
            "map": map_ref,
            "configVersion": _hash(configuration),
            "motionConfig": configuration,
            "destinations": destinations,
        }

    @_storage_errors
    def catalog(self, session: str) -> dict[str, object]:
        with self._lock:
            now = self._now()
            return {
                "status": "ready",
                "reason": None,
                "catalog": self._catalog(session, now),
                "serverNowMs": now,
            }

    def resolve(self, session: str, raw: object) -> dict[str, object]:
        """Resolve names/aliases for a compiler without inventing coordinates."""
        request = _exact(_copy(raw), {"query", "selected"})
        query = _normalized(_text(request["query"]))
        selected = _targets(request["selected"], allow_empty=True)
        catalog = self.catalog(session)["catalog"]
        candidates = [
            zone
            for zone in catalog["destinations"]
            if query
            in {
                _normalized(zone["zoneId"]),
                _normalized(zone["name"]),
                *(_normalized(alias) for alias in zone["aliases"]),
            }
        ]
        if len(candidates) > 1:
            return {"kind": "ambiguous", "candidates": candidates}
        if not candidates:
            return {
                "kind": "refused",
                "code": "destination_unknown",
                "reason": "No accepted destination matches that name or alias.",
            }
        destination = candidates[0]
        if destination["excluded"]:
            return {
                "kind": "refused",
                "code": "destination_excluded",
                "reason": "That named area is excluded by the accepted map.",
            }
        if any(item["deviceClass"] not in destination["allowedClasses"] for item in selected):
            return {
                "kind": "refused",
                "code": "destination_class_unsupported",
                "reason": "The destination does not allow every selected device class.",
            }
        return {"kind": "resolved", "destination": destination}

    @_storage_errors
    def compile(self, session: str, raw: object) -> dict[str, object]:
        """Compile an explicit destination reference into this review workflow.

        The caller supplies a destination name, not a transcript granting a
        sequence of other actions. Ambiguity produces clarification; only an
        exact accepted identity can become a navigate argument. The compiler
        never expands the selection or creates a generic executable voice plan.
        """
        request = _exact(_copy(raw), {"intentId", "query"})
        _identity(request["intentId"])
        _text(request["query"])
        with self._lock:
            catalog = self._catalog(session, self._now())
            state = self._context(session, catalog)["state"]
            selected = state["selected"]
            if not selected:
                return {
                    "kind": "refused",
                    "code": "no_selection",
                    "reason": "Select devices before reviewing a destination.",
                }
            resolution = self.resolve(session, {"query": request["query"], "selected": selected})
            if resolution["kind"] == "ambiguous":
                return {
                    "kind": "clarify",
                    "code": "destination_ambiguous",
                    "candidates": resolution["candidates"],
                }
            if resolution["kind"] != "resolved":
                return resolution
            zone_id = resolution["destination"]["zoneId"]
            preview_request = {
                "session": session,
                "intentId": request["intentId"],
                "zoneId": zone_id,
                "rosterVersion": state["rosterVersion"],
                "selected": selected,
                **{
                    key: catalog[key]
                    for key in (
                        "catalogVersion",
                        "map",
                        "configVersion",
                        "motionConfig",
                    )
                },
            }
            return {
                "kind": "review",
                "intent": {
                    "name": "navigate",
                    "args": {"zone_id": zone_id},
                    "selection": [target["id"] for target in selected],
                    "mode": "indoor",
                },
                **self.preview(session, preview_request),
            }

    def _context(self, session: str, catalog: dict) -> dict[str, object]:
        raw_state = self.state(session)
        self.observe_state(session, raw_state)
        state = state_projection(raw_state)
        if state["session"] != session:
            _fail("session_changed", "The live state belongs to another session.")
        generation = self._db.execute(
            "SELECT generation FROM navigation_state_generations WHERE session=?", (session,)
        ).fetchone()[0]
        return {
            "bootId": self._boot_id,
            "generation": generation,
            "state": state,
            "catalog": {
                key: value
                for key, value in catalog.items()
                if key not in {"receivedAt", "expiresAt"}
            },
        }

    @_storage_errors
    def preview(self, session: str, raw: object) -> dict[str, object]:
        request = _exact(
            _copy(raw),
            {
                "session",
                "intentId",
                "zoneId",
                "rosterVersion",
                "selected",
                "catalogVersion",
                "map",
                "configVersion",
                "motionConfig",
            },
        )
        _identity(request["intentId"])
        _identity(request["zoneId"])
        _integer(request["rosterVersion"])
        selected = _targets(request["selected"])
        with self._lock:
            now = self._now()
            catalog = self._catalog(session, now)
            context = self._context(session, catalog)
            state = context["state"]
            if request["session"] != session:
                _fail("session_changed", "The request belongs to another session.")
            for key in ("catalogVersion", "map", "configVersion", "motionConfig"):
                if request[key] != catalog[key]:
                    _fail("frozen_inputs_changed", f"The current {key} differs from the request.")
            if request["rosterVersion"] != state["rosterVersion"]:
                _fail("roster_changed", "The authoritative roster changed before review.")
            if selected != state["selected"]:
                _fail("selection_changed", "The selected device classes or epochs changed.")
            destination = next(
                (zone for zone in catalog["destinations"] if zone["zoneId"] == request["zoneId"]),
                None,
            )
            if destination is None:
                _fail("destination_unknown", "The canonical destination is absent from this map.")
            nodes = {node["target"]["id"]: node for node in state["nodes"]}
            outcomes = []
            for target in selected:
                node = nodes[target["id"]]
                code, detail = self._node_refusal(target, node, state, destination)
                outcomes.append(
                    {"target": target, "status": "refused", "code": code, "detail": detail}
                )
            routes = []
            eligible = [item for item in outcomes if item["code"] == "class_planner_unavailable"]
            if self.route_preview is not None and len(eligible) == len(selected):
                planned = _copy(self.route_preview(_copy(request), _copy(catalog), _copy(state)))
                routes, outcomes = validate_route_preview(planned, selected, destination)
            preview = {
                "previewId": str(uuid.uuid4()),
                "session": session,
                "intentId": request["intentId"],
                "rosterVersion": request["rosterVersion"],
                "selected": selected,
                "destination": destination,
                "map": catalog["map"],
                "catalogVersion": catalog["catalogVersion"],
                "configVersion": catalog["configVersion"],
                "motionConfig": catalog["motionConfig"],
                "routes": routes,
                "outcomes": outcomes,
                "receivedAt": now,
                "expiresAt": now + self.review_ttl_ms,
                "dispatchEligible": False,
            }
            if (
                self.flight_execution is not None
                and len(selected) == 1
                and selected[0]["deviceClass"] == "aircraft"
            ):
                try:
                    execution = _copy(self.flight_execution.preview(session, _copy(preview)))
                    routes, outcomes, execution = validate_flight_execution_preview(
                        execution, selected, destination, preview["map"]
                    )
                except (ValueError, KeyError, TypeError) as error:
                    _fail(
                        "navigation_execution_unavailable",
                        str(error) or "Qualified aircraft navigation is unavailable.",
                    )
                preview.update(
                    destination={**destination, "reachability": "reachable"},
                    routes=routes,
                    outcomes=outcomes,
                    execution=execution,
                    dispatchEligible=True,
                )
            digest = _hash(preview)
            # Re-read every authoritative input after provider work. A late route
            # result cannot become a review for a changed roster/map/configuration.
            current = self._context(session, self._catalog(session, self._now()))
            if current != context:
                _fail("frozen_inputs_changed", "Authoritative inputs changed during route review.")
            with self._db:
                self._db.execute("DELETE FROM navigation_previews WHERE expires_at <= ?", (now,))
                if (
                    self._db.execute("SELECT COUNT(*) FROM navigation_previews").fetchone()[0]
                    >= self.max_previews
                ):
                    _fail("review_capacity", "Too many unexpired destination reviews are retained.")
                try:
                    self._db.execute(
                        "INSERT INTO navigation_previews VALUES (?,?,?,?,?,?,?,?,0)",
                        (
                            preview["previewId"],
                            session,
                            request["intentId"],
                            now,
                            preview["expiresAt"],
                            _json(preview),
                            digest,
                            _hash(context),
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise NavigationError(
                        "intent_reused", "Use a new intent ID for each review."
                    ) from error
            return {"preview": _copy(preview), "previewHash": digest, "serverNowMs": self._now()}

    @staticmethod
    def _node_refusal(target: dict, node: dict, state: dict, destination: dict) -> tuple[str, str]:
        if state["estop"]:
            return "estop_active", "The emergency stop is active."
        if state["mode"] != "indoor":
            return "mode_unsupported", "Known-map navigation requires the indoor mode."
        if not node["ready"] or not node["authority"]:
            return (
                "node_not_ready",
                "Current telemetry, readiness and control authority are required.",
            )
        if target["deviceClass"] == "aircraft" and node["motionState"] not in {
            "airborne",
            "hovering",
        }:
            return (
                "aircraft_grounded",
                "Navigation never grants takeoff; the aircraft must already be airborne.",
            )
        if target["deviceClass"] not in destination["allowedClasses"]:
            return "destination_class_unsupported", "The destination excludes this device class."
        if destination["excluded"]:
            return "destination_excluded", "The destination is excluded from navigation."
        if destination["reachability"] == "unreachable":
            return (
                "destination_unreachable",
                "The class planner reports the destination unreachable.",
            )
        if "navigate" not in state["enabledIntentNames"] or "navigate" not in node["capabilities"]:
            return (
                "capability_disabled",
                "Navigation is not advertised by both the relay and this device.",
            )
        return (
            "class_planner_unavailable",
            "No qualified class route, world pose and arrival allocation are available.",
        )

    @_storage_errors
    def confirm(self, session: str, raw: object) -> dict[str, object]:
        request = _exact(_copy(raw), {"previewId", "intentId", "previewHash"})
        _identity(request["previewId"])
        _identity(request["intentId"])
        if (
            type(request["previewHash"]) is not str
            or _HASH.fullmatch(request["previewHash"]) is None
        ):
            _fail("invalid_payload", "The confirmation requires its captured server preview hash.")
        with self._lock:
            row = self._db.execute(
                """SELECT session,intent_id,created_at,expires_at,
                preview_json,preview_hash,context_hash,consumed FROM navigation_previews
                WHERE preview_id=?""",
                (request["previewId"],),
            ).fetchone()
            if row is None or row[0] != session:
                _fail("preview_unknown", "No matching session-bound review exists.")
            try:
                retained = json.loads(row[4])
                if _hash(retained) != row[5] or any(
                    retained.get(key) != expected
                    for key, expected in (
                        ("previewId", request["previewId"]),
                        ("session", row[0]),
                        ("intentId", row[1]),
                        ("receivedAt", row[2]),
                        ("expiresAt", row[3]),
                    )
                ):
                    raise ValueError("retained review mismatch")
            except (ValueError, TypeError, AttributeError) as error:
                raise NavigationError(
                    "storage_unavailable",
                    "Retained navigation evidence failed its integrity check.",
                    503,
                ) from error
            if row[1] != request["intentId"] or row[5] != request["previewHash"]:
                _fail("confirmation_mismatch", "Confirmation does not bind the captured review.")
            code, detail = (
                "navigation_execution_unavailable",
                "Class-qualified navigation execution is not enabled.",
            )
            status = "refused"
            dispatch_eligible = False
            now = self._now()
            current = False
            if row[7]:
                code, detail, status = (
                    "confirmation_consumed",
                    "This review has already been consumed.",
                    "invalidated",
                )
            elif now < row[2] or now >= row[3]:
                code, detail, status = (
                    "preview_expired",
                    "The captured review is no longer current.",
                    "invalidated",
                )
            else:
                try:
                    context = self._context(session, self._catalog(session, now))
                    current = _hash(context) == row[6]
                    if not current:
                        code, detail, status = (
                            "frozen_inputs_changed",
                            "Authoritative review inputs changed.",
                            "invalidated",
                        )
                except (NavigationError, ValueError):
                    code, detail, status = (
                        "frozen_inputs_changed",
                        "Authoritative review evidence is unavailable.",
                        "invalidated",
                    )
            with self._db:
                self._db.execute(
                    "UPDATE navigation_previews SET consumed=1 WHERE preview_id=?",
                    (request["previewId"],),
                )
            if current and retained.get("dispatchEligible") is True:
                if self.flight_execution is None:
                    code, detail = (
                        "navigation_execution_unavailable",
                        "Class-qualified navigation execution is not enabled.",
                    )
                else:
                    try:
                        dispatched = _exact(
                            _copy(self.flight_execution.confirm(session, _copy(retained))),
                            {"status", "code", "detail"},
                        )
                        if (
                            dispatched["status"] != "accepted"
                            or not isinstance(dispatched["code"], str)
                            or _IDENTITY.fullmatch(dispatched["code"]) is None
                            or not isinstance(dispatched["detail"], str)
                        ):
                            raise ValueError("qualified navigation dispatch response is invalid")
                        status, code, detail, dispatch_eligible = (
                            "accepted",
                            dispatched["code"],
                            _text(dispatched["detail"], 2048),
                            True,
                        )
                    except (ValueError, KeyError, TypeError) as error:
                        code, detail = (
                            "navigation_dispatch_failed",
                            str(error) or "Qualified navigation dispatch failed.",
                        )
            return {
                "status": status,
                "code": code,
                "detail": detail,
                "previewId": request["previewId"],
                "intentId": request["intentId"],
                "dispatchEligible": dispatch_eligible,
            }


def validate_flight_execution_preview(
    raw: object, selected: list[dict], destination: dict, map_ref: object
) -> tuple[list, list, dict]:
    if len(selected) != 1 or selected[0].get("deviceClass") != "aircraft":
        _fail("planner_contract_invalid", "Qualified aircraft execution requires one aircraft.")
    result = _exact(_copy(raw), {"routes", "outcomes", "execution"})
    routes, outcomes = validate_route_preview(
        {"routes": result["routes"], "outcomes": result["outcomes"]}, selected, destination
    )
    execution = _exact(
        result["execution"],
        {
            "planHash",
            "mapPin",
            "geometryPin",
            "navigationPin",
            "approvalId",
            "configurationSha256",
            "permissionZoneIds",
        },
    )
    if (
        type(execution["planHash"]) is not str
        or _HASH.fullmatch(execution["planHash"]) is None
        or type(execution["configurationSha256"]) is not str
        or _HASH.fullmatch(execution["configurationSha256"]) is None
        or not isinstance(map_ref, dict)
        or execution["mapPin"] != map_ref.get("mapPin")
    ):
        _fail("planner_contract_invalid", "Qualified execution pins do not bind the approved map.")
    for name in ("geometryPin", "navigationPin"):
        value = execution[name]
        if (
            type(value) is not dict
            or set(value) != {"version", "contentSha256"}
            or not isinstance(value["version"], str)
            or _IDENTITY.fullmatch(value["version"]) is None
            or type(value["contentSha256"]) is not str
            or _HASH.fullmatch(value["contentSha256"]) is None
        ):
            _fail("planner_contract_invalid", "Qualified execution pins are invalid.")
    _identity(execution["approvalId"])
    permissions = execution["permissionZoneIds"]
    if (
        type(permissions) is not list
        or not permissions
        or len(permissions) > MAX_DESTINATIONS
        or permissions != sorted(set(permissions))
    ):
        _fail("planner_contract_invalid", "Qualified execution permissions are invalid.")
    for zone_id in permissions:
        _identity(zone_id)
    if destination["zoneId"] not in permissions:
        _fail("planner_contract_invalid", "Qualified execution does not permit the destination.")
    if len(routes) != len(selected) or any(outcome["status"] != "planned" for outcome in outcomes):
        _fail(
            "planner_contract_invalid", "Qualified aircraft execution requires every target route."
        )
    return routes, outcomes, _copy(execution)


def validate_route_preview(
    raw: object, selected: list[dict], destination: dict
) -> tuple[list, list]:
    """Bound a provider's routes and per-node outcomes without trusting UI data."""
    result = _exact(raw, {"routes", "outcomes"})
    routes, outcomes = result["routes"], result["outcomes"]
    if type(routes) is not list or len(routes) > len(selected):
        _fail("planner_contract_invalid", "Class planner route count is invalid.")
    if type(outcomes) is not list or len(outcomes) != len(selected):
        _fail("planner_contract_invalid", "Class planner must report every selected device.")
    targets = {target["id"]: target for target in selected}
    planned = set()
    seen = set()
    for outcome in outcomes:
        _exact(outcome, {"target", "status", "code", "detail"})
        target = _targets([outcome["target"]])[0]
        if targets.get(target["id"]) != target or target["id"] in seen:
            _fail("planner_contract_invalid", "A class planner outcome has the wrong target.")
        seen.add(target["id"])
        if outcome["status"] not in {"planned", "refused"}:
            _fail("planner_contract_invalid", "A class planner outcome has an invalid status.")
        _identity(outcome["code"])
        _text(outcome["detail"], 512)
        if outcome["status"] == "planned":
            planned.add(target["id"])
    route_ids, slots = set(), set()
    total = 0
    for route in routes:
        _exact(route, {"target", "waypoints", "arrivalSlot", "holdBehavior"})
        target = _targets([route["target"]])[0]
        if targets.get(target["id"]) != target or target["id"] in route_ids:
            _fail("planner_contract_invalid", "A class planner route has the wrong target.")
        route_ids.add(target["id"])
        if route["holdBehavior"] != ("hover" if target["deviceClass"] == "aircraft" else "stop"):
            _fail("planner_contract_invalid", "Arrival hold behavior does not match device class.")
        points = route["waypoints"]
        if type(points) is not list or not 2 <= len(points) <= 512:
            _fail(
                "planner_contract_invalid", "A route requires bounded start and arrival waypoints."
            )
        total += len(points)
        for point in points:
            _exact(point, {"xM", "yM", "zM", "floorId", "frame"})
            if point["floorId"] != destination["floorId"] or point["frame"] != "world":
                _fail("wrong_floor", "The route must remain in the accepted world map and floor.")
            if any(
                type(point[key]) not in {int, float}
                or not math.isfinite(point[key])
                or abs(point[key]) > 1_000_000
                for key in ("xM", "yM", "zM")
            ):
                _fail("planner_contract_invalid", "Route coordinates exceed the rendering bound.")
        slot = _exact(route["arrivalSlot"], {"slotId", "zoneId", "position"})
        _identity(slot["slotId"])
        if (
            slot["slotId"] in slots
            or slot["zoneId"] != destination["zoneId"]
            or slot["position"] != points[-1]
        ):
            _fail(
                "planner_contract_invalid",
                "Arrival slots must be distinct and bind the route endpoint.",
            )
        slots.add(slot["slotId"])
    if route_ids != planned or total > 8192:
        _fail(
            "planner_contract_invalid",
            "Routes must match all and only the planned device outcomes.",
        )
    return _copy(routes), _copy(outcomes)
