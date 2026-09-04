# relay

Capability area: Platform. Milestone: M1.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to shared contracts or the authoritative relay state shape name one change owner and require cross-review.

The intent bus. FastAPI with a WebSocket endpoint that accepts intents from authenticated, source-bound clients, stamps and logs them to append-only JSONL, forwards them to the planner, holds the authoritative swarm state, and fans state and telemetry out to consoles at 10 Hz. Exposes `/metrics` and `/session/<id>` for replay. A process restart closes prior live session IDs rather than guessing how to reconstruct safety state; their logs remain available for authenticated replay.

PRD: sections 4.2, 5.2, Appendix A (intent contract), Appendix B (telemetry and state fan-out).

Run from the repo root: `uv run python -m relay.<module>`.

## Intent v1 contract

`relay.intent_v1.validate_intent(raw)` is the shared validation seam. It returns `AcceptedIntent` with an immutable `IntentV1`, or `RejectedIntent` with one of four reasons: `invalid_payload`, `unknown_source`, `unknown_intent`, or `unsupported`. Input failures are values, so callers do not catch validation exceptions or parse error text.

The validator makes these schema choices where Appendix A leaves details open:

- Every displayed field except `retry_of` is required, and extra top-level fields are rejected. Initial requests may omit `retry_of` or set it to null.
- `t` is a non-negative integer timestamp in epoch milliseconds. Freshness checks belong to the relay session path.
- `session` is an opaque, non-empty string.
- Drone IDs are unique positive integers. The current `selection` may be empty; `select.args.ids` may not.
- Motion values are finite JSON numbers in planner-owned steps. The validator does not convert them to metres or impose mode bounds.
- `intent_id` is a non-empty stable identifier. A retry gets a new identifier and may link to a different request through `retry_of`. This function validates the reference shape; the relay lifecycle validates same-session failure, deduplication, and terminal-state semantics.
- `confirm` records the source's confirmation state. `capture_room` requires confirmation and exactly one selected drone; the arbiter enforces the remaining action-specific checks.
- Rejection precedence is envelope, registered source, intent name, argument shape, scope, mode capability, then intent-name capability.
- M2.0 accepts indoor requests for the eight flight-control names plus the previously accepted `capture_room` path. The outdoor mode values remain schema-reserved and return `unsupported`; the remaining intent names keep their v1 argument shapes and also return `unsupported`.
- `come_home` returns selected drones to their home positions through planner-generated `goto` calls. The separate confirmed `land_all` intent maps to adapter `land`.

The current source registry is `console` and `keyboard`. Language and webcam join only when their real producers and conformance tests land. Registering another source or enabling another Intent v1 name changes the shared constants and conformance tests in this module.

## Run the relay

Install the locked environment, copy `.env.example` to the git-ignored `.env`, and export its values. `SWEEP_RELAY_TOKEN` authenticates the console, keyboard client, HTTP metrics, and replay. It must contain at least 32 characters. Configure independent adapter credentials as a JSON object keyed by stable positive drone ID:

```dotenv
SWEEP_RELAY_TOKEN=<at-least-32-characters>
SWEEP_ADAPTER_KEYS_JSON={"1":"<adapter-1-key-at-least-32-characters>"}
SWEEP_ALLOW_SHARED_ADAPTER_TOKEN=false
```

`SWEEP_ALLOW_SHARED_ADAPTER_TOKEN=true` is a demo-only fallback. It proves that a frame came from a holder of the shared secret, but cannot prove which aircraft sent it; keep it false for hardware. The freshness settings in `.env.example` are explicit demo values and must be measured and configured for a hardware session.

Run loopback by default. An intentional LAN deployment should add its transport protection and network boundary outside the app:

```bash
uv sync --locked
uv run uvicorn relay.app:app --host 127.0.0.1 --port 8000
```

## Authentication and connection binding

Connect to `/ws/{session_id}`. The first and only unauthenticated frame is one of:

```json
{"v":1,"type":"auth","source":"console","token":"..."}
{"v":1,"type":"auth","source":"keyboard","token":"..."}
{"v":1,"type":"auth","source":"adapter","drone_id":1,"token":"..."}
```

The first successful server event is `auth.accepted`; the browser must not mark itself connected or authenticated before it receives this event. It is followed by the current state.

```json
{"v":1,"t":1756700000000,"type":"auth.accepted","event_id":"...","session":"demo","source":"console","drone_id":null}
```

`auth.refused` contains `event_id`, `session`, `status: "refused"`, and machine-readable `reason` plus display-only `detail`, then the server closes with policy code 1008. Auth frames, credentials, and signatures are never written to the audit log. After authentication, an Intent v1 `source` must exactly equal the bound `console` or `keyboard` source. An adapter connection is bound to one configured `drone_id`, and a second live connection for that ID is refused.

## Adapter frames and signed membership

Membership frames have common fields `v`, `t`, `type: "membership"`, `event_id`, `session`, `drone_id`, `action`, and `signature`. Actions add these exact fields:

- `join`: `adapter_id` and a non-empty unique `capabilities` list. The checkpoint readiness minimum includes the exact `flight` capability; camera patterns may be `pano_360`, `reconstruct_8`, or namespaced as `camera:<pattern>`.
- `readiness`: `connection_epoch`, `home_pose_confirmed`, `control_authority`, and `rc_safety_operator_present`. When confirmed, home XYZ is captured from the current-epoch telemetry already held by the relay.
- `graceful_leave`: `connection_epoch`.

Compute `signature` as lowercase HMAC-SHA256 using that aircraft's configured key over the object with `signature` omitted, UTF-8 encoded with JSON object keys sorted, no insignificant whitespace, and non-ASCII characters unescaped. Signed membership claims deliberately contain no floats, avoiding cross-language numeric canonicalization. `relay.auth.sign_event` is the reference implementation.

The relay never accepts an adapter-authored `unexpected_loss`. Only closure of the already authenticated, drone-bound socket can establish transport loss; the relay then creates the membership event with `provenance: "relay_transport_attestation"`. This is the trusted relay-originated equivalent of a signed unexpected-loss claim and prevents one adapter from disconnecting another by message. Signed adapter events use `provenance: "adapter_signature"`. Signatures themselves are verified before transition and omitted from logs and fan-out.

Telemetry extends Appendix B with transport identity and ordering fields:

```json
{"v":1,"t":1756700000000,"type":"telemetry","event_id":"...","session":"demo","drone":1,"connection_epoch":2,"x":1.2,"y":-0.4,"z":1.0,"vx":0.0,"vy":0.0,"vz":0.0,"battery":0.72,"state":"hovering","link":0.95,"pos_quality":0.9}
```

Telemetry and adapter acknowledgements rely on the authenticated drone binding and current `connection_epoch`. Duplicate event IDs, regressive timestamps, stale frames, wrong sessions, wrong drone IDs, and prior epochs are refused. Every adapter-authored acknowledgement is command-scoped and must carry a non-null, non-empty `command_id`; only relay/orchestrator lifecycle events may leave it null:

```json
{"v":1,"t":1756700000001,"type":"acknowledgement","event_id":"...","session":"demo","intent_id":"intent-1","command_id":"command-1","status":"executing","source":"adapter","drone_id":1,"connection_epoch":2,"roster_version":4,"reason":null,"detail":null}
```

Adapter command acknowledgements are audit facts; they never complete the overall intent. The autonomy owner reports the terminal intent result through `RelaySession.record_lifecycle`. Lifecycle values are exactly `accepted`, `refused`, `executing`, `completed`, `failed`, and `invalidated`. Reasons are machine-readable snake_case; detail is display-only.

An Intent v1 request is acknowledged as `accepted` only after the configured `intent_sink_factory` hands it to a planner/arbiter consumer. The standalone relay intentionally returns `downstream_unavailable`; it never claims that Hold, E-stop, or another action entered an execution path when no consumer is configured. A sink exception produces a terminal `downstream_error` refusal and matching replay records.

## Membership and state fan-out

Every accepted membership transition is immediately followed, in the same ordered publication, by a `state` event. Membership values are exactly `registered`, `ready`, `leaving`, `disconnected`, and `degraded`. A session retains records and membership history for disconnected aircraft, caps physical stable IDs at four, increments `connection_epoch` on rejoin, and increments `roster_version` on membership changes. Join and rejoin do not modify the current selection or accepted plan.

`graceful_leave` defaults closed. Integration must provide `leave_authorizer_factory` to `create_app`; its per-session callback receives `(drone_id, connection_epoch, current_state)` and returns true only after the autonomy path proves landed, disarmed, and task-free. Without that approval, the relay emits `graceful_leave_not_authorized`. After approval, the registry atomically removes the aircraft from selection and clears pending confirmation and the accepted prior-roster plan while entering `leaving`. That membership event is followed by a one-shot state carrying `invalidated_intent_ids`, `invalidation_reason: "graceful_leave_roster_change"`, `prior_roster_version`, and `cleared_control_fields`; periodic states do not repeat this transition metadata. A socket closing without an authorized leave is recorded as unexpected loss.

State is fanned out at 10 Hz. Its required top-level keys are:

```text
v, t, type="state", event_id, session, roster_version, armed, estop,
selection, formation, spacing, mode, pending, accepted_plan, drones
```

Each drone has these required keys:

```text
drone_id, connection_epoch, membership, readiness_reasons, flight_state,
battery, link, pos_quality, control_authority, last_seen_at, camera_patterns,
selectable, adapter_id, adapter_capabilities, home_pose,
rc_safety_operator_present, telemetry, membership_history
```

`flight_state`, battery/link/position aliases, and `last_seen_at` are a normalized console projection and are nullable until current telemetry exists. The nested `telemetry` object is the authoritative Appendix B snapshot; its transport-only event ID, session, and connection epoch are represented by the containing drone/event. `camera_patterns` is derived from the signed capability list and does not assert camera readiness. The relay does not invent storage, camera-ready, active-task, or operator-presence/timing facts absent from Appendix B. The autonomy boundary must enrich those inputs explicitly and fail closed when they are missing.

Top-level `armed` is the authoritative session arm authorization, initially false and updated only through `RelaySession.update_control_projection(armed=...)` after the planner/arbiter accepts that control-state change. It is not inferred from aircraft flight-state strings. Join and rejoin leave it unchanged; a new session after process restart begins disarmed. Per-aircraft physical armed/disarmed evidence remains an explicit autonomy enrichment used by graceful-removal safety.

Server WebSocket event types are `auth.accepted`, `auth.refused`, `membership`, `state`, `telemetry`, `acknowledgement`, and `refusal`; every one carries `event_id`. A refusal always includes all of `intent_id`, `command_id`, `drone_id`, `connection_epoch`, `roster_version`, `reason`, and `detail`; context fields are deliberately present as null when they do not apply. Acknowledgements use the same always-present context fields; `command_id` is non-null for adapter facts and nullable for relay/orchestrator intent-level lifecycle events.

## Audit and replay

Each normalized event is one append-only JSONL record shaped as `{"seq": N, "event": {...}}`, with a contiguous per-session sequence. Session names are SHA-256 hashed for filenames under `SWEEP_SESSION_LOG_DIR`. Any attempt to log a token, signature, authorization value, credential, password, or secret is rejected recursively.

Events from one relay operation are committed as a single audit batch. A durable pending-operation cursor lets restart recovery discard a complete-record prefix when an interrupted batch did not reach its operation boundary, while replay keeps the same per-event record shape.

Authenticate HTTP requests with `Authorization: Bearer $SWEEP_RELAY_TOKEN`. `GET /metrics` returns relay/session counters. `GET /session/{id}?after_sequence=N` returns a `replay` envelope whose `events` are the ordered JSONL records after `N`. `intent_record` is log-only and pairs the normalized Intent v1 request with its accepted/refused outcome; membership, telemetry, state, acknowledgement, and refusal records use the same event shapes delivered live. Replay UI remains outside M2.0.

A live session ID is scoped to one relay process lifetime. After restart, any ID whose persisted log is nonempty is replay-only: a correctly authenticated WebSocket receives `auth.refused` with `reason: "session_closed"` and must reconnect under a new session ID. The replay endpoint reads that closed log without constructing mutable fleet state. This deliberately prevents a restarted relay from appending new roster versions or connection epochs starting at one; safe live-state restoration also requires autonomy-owned plan/confirmation invalidation and loss handling and is not part of M1.1.

On reopen, the relay removes only a nonempty, unterminated EOF fragment after validating every complete JSONL record before it. The repair is fsynced and replay preserves the valid contiguous prefix. Complete malformed records and corruption before the EOF fragment still fail closed without changing the file. A repaired log remains evidence of prior session use even when its first record was torn, so that session ID stays replay-only.
