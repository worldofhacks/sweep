# relay

Capability area: Platform. Milestone: M1.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to shared contracts or the authoritative relay state shape name one change owner and require cross-review.

The intent bus. FastAPI with a WebSocket endpoint that accepts intents from authenticated, source-bound clients, stamps and logs them to append-only JSONL, forwards them to the planner, holds the authoritative swarm state, and fans state and telemetry out to consoles at 10 Hz. Exposes `/metrics` and `/session/<id>` for replay. A process restart closes prior live session IDs rather than guessing how to reconstruct safety state; their logs remain available for authenticated replay.

PRD: sections 4.2, 5.2, Appendix A (intent contract), Appendix B (telemetry and state fan-out).

Run from the repo root: `uv run python -m relay.<module>`.

## Intent v1 contract

`relay.intent_v1.validate_intent(raw)` is the shared validation seam. It returns `AcceptedIntent` with an immutable `IntentV1`, or `RejectedIntent` with one of five reasons: `invalid_payload`, `unknown_source`, `unknown_intent`, `unsupported`, or `source_not_allowed`. Input failures are values, so callers do not catch validation exceptions or parse error text.

The validator makes these schema choices where Appendix A leaves details open:

- Every displayed field except `retry_of` is required, and extra top-level fields are rejected. Initial requests may omit `retry_of` or set it to null.
- `t` is a non-negative integer timestamp in epoch milliseconds. Freshness checks belong to the relay session path.
- `session` is an opaque, non-empty string.
- Drone IDs are unique positive integers. The current `selection` may be empty; `select.args.ids` may not.
- Motion values are finite JSON numbers in planner-owned steps. The validator does not convert them to metres or impose mode bounds.
- `intent_id` is a non-empty stable identifier. A retry gets a new identifier and may link to a different request through `retry_of`. This function validates the reference shape; the relay lifecycle validates same-session failure, deduplication, and terminal-state semantics.
- `confirm` records the source's confirmation state. `capture_room` requires confirmation and exactly one selected drone; the arbiter enforces the remaining action-specific checks.
- Rejection precedence is envelope, registered source, intent name, argument shape, scope, mode capability, intent-name capability, then the per-source allowlist.
- `c1_basic_control` enables `arm`, `select`, `takeoff`, `translate`, `hold`, `come_home`, `land`, `land_all`, `estop`, `capture_room`, `altitude`, `formation_next`, `formation_set`, `spacing`, and `sweep`. The outdoor mode values remain schema-reserved and return `unsupported`; `disarm`, `survey_area`, and `map_area` keep their v1 argument shapes and also return `unsupported`.
- `come_home` returns selected drones to their home positions through planner-generated `goto` calls. Confirmed `land` maps the current selection to adapter `land`; `land_all` applies landing fleet-wide.

The current source registry is `console`, `keyboard`, and `webcam`; `webcam` is the console-hosted gesture producer and authenticates on its own connection with the same relay token. Each source may emit only the names in `SOURCE_ALLOWED_NAMES`: `console` every implemented name allowed by the effective capability profile, `keyboard` only `estop` (the Shift+Escape network stop), and `webcam` only `capture_room` and `hold`, the two names the console gesture policy may draft, so its never-gesture-emittable list is enforced on both sides. A profile-disabled name is refused as `unsupported`; a profile-enabled name outside its source's set is refused with `source_not_allowed`, and the detail names the intent and source. The session uses the same reason for a connection that cannot emit intents at all. Language joins only when its real producer and conformance tests land. Registering another source, implementing another Intent v1 name, or widening a source's allowlist changes the shared constants and conformance tests in this module.

## Run the relay

Install the locked environment, copy `.env.example` to the git-ignored `.env`, and export its values. `SWEEP_RELAY_TOKEN` authenticates the console, keyboard client, HTTP metrics, and replay. It must contain at least 32 characters. Configure independent adapter credentials as a JSON object keyed by stable positive drone ID:

```dotenv
SWEEP_RELAY_TOKEN=<at-least-32-characters>
SWEEP_ADAPTER_KEYS_JSON='{"1":"<adapter-1-key-at-least-32-characters>"}'
SWEEP_ALLOW_SHARED_ADAPTER_TOKEN=false
```

Single-quote JSON values in `.env`: `just relay` and `just fake-node` read the file with `uv run --env-file`, which strips double quotes from unquoted values.

`SWEEP_ALLOW_SHARED_ADAPTER_TOKEN=true` is a demo-only fallback. It proves that a frame came from a holder of the shared secret, but cannot prove which aircraft sent it; keep it false for hardware. The freshness settings in `.env.example` are explicit demo values and must be measured and configured for a hardware session.

## Voice transcription

`POST /api/sessions/{id}/transcripts` accepts a bearer-authenticated `audio/webm`, `audio/ogg`, `audio/wav`, or `audio/mpeg` body up to 8 MiB and 30 seconds. The browser reports duration in `X-Sweep-Audio-Duration-Ms`; the relay rejects an oversized declaration and independently decodes the audio before provider I/O so a false or missing header cannot bypass the limit. Audio that cannot be decoded, lacks a sample rate, or carries negative, repeated, or non-monotonic frame timestamps is refused. The request carries a bounded `X-Sweep-Correlation-Id`. The relay reads `OPENAI_API_KEY` only on the server and sends valid uploads to OpenAI's `whisper-1` endpoint. Browser code never receives that credential.

Browser uploads are allowed only from the explicit origins in `SWEEP_CONSOLE_ORIGINS`, which defaults to the local Vite development origins. Configure the deployed console origin rather than using a wildcard.

The endpoint requires an existing live relay session. It derives the compiler capability version from the authoritative state projection, hands the final transcript to `TranscriptCompiler.compile(transcript, relay_state, capability_version=..., rooms=..., now_ms=..., correlation_id=..., session_id=...)`, and returns a typed `voice_outcome`. Because transcription takes seconds, the state handed to the compiler is re-read after the transcript arrives, so the compiler's two-second maximum state age is measured against the plan rather than the upload. Upload, provider, and compiler failures use the same no-emission shape. The standalone `relay.app:app` keeps the compiler handoff unavailable and returns `compiler_unavailable` with `emissions: []`; `relay.main` wires the pinned compiler (below).

### Compiled plan preview

`relay.main` builds the voice service with `build_transcript_service`: `language.relay_compiler.RelayTranscriptCompiler` binds one `language.compiler.TranscriptCompiler` per session to that session's append-only log through `SessionCompilerAudit`, with the planner's translation policy and the composition's capability profile, a 30 second plan TTL, and a 2 second relay-state maximum age. Two keys, both read only in the relay process, enable the two steps. `OPENAI_API_KEY` enables Whisper: without it every upload is refused `transcription_unavailable` and the console shows "Transcription is unavailable on the relay" while typed text still compiles locally. `ANTHROPIC_API_KEY` enables the compiler: without it (or when the provider is unreachable) the endpoint returns the transcript with the typed `compiler_unavailable` refusal and no plan, and the console compiles the transcript with its local fallback, labelled `compiled by local fallback · relay compiler compiler_unavailable`. Neither absence is a crash, and neither path emits.

`voice_outcome` carries a versioned `plan` field (`null` in the original shape, which the console still renders). When present, `plan` is the compiler's validated preview and never an emitted intent:

```json
{
  "v": 1,
  "kind": "plan",
  "transcript": "Take off.",
  "reason": null,
  "detail": null,
  "options": [],
  "steps": [
    {
      "index": 0,
      "name": "takeoff",
      "args": {},
      "selection": [1],
      "mode": "indoor",
      "confirm_required": true,
      "notes": ["Targets D-01 (the current selection).", "..."]
    }
  ],
  "compiled_at_ms": 1756700003000,
  "expires_at_ms": 1756700033000,
  "state_event_id": "…",
  "roster_version": 2,
  "session": "demo",
  "correlation_id": "…",
  "plan_digest": "…",
  "model": "claude-sonnet-5",
  "prompt_schema_version": "intent-v1-compiler-8",
  "response_source": "anthropic",
  "pending_intent_id": null
}
```

`kind` is `plan` (ordered Intent v1 drafts with the arbiter's confirmation requirement and the compiler's deterministic grounding notes per step; `expires_at_ms` and `plan_digest` are set), `clarify` (a typed `reason` plus `options` such as the authoritative rooms or the selectable aircraft; no steps), `unsupported` or `refuse` (a typed `reason` from the language package's `CompilerReason` and optional `detail`), or `cancel_pending` (names the pending intent). Every plan is bound to the `state_event_id` and `roster_version` it was grounded on. `relay.voice.parse_voice_outcome` and `parse_voice_plan` are the relay-side validators; `console/src/relay/contract.ts` `isVoicePlan` and `console/src/voice/client.ts` mirror them. The console stages each step through its own control flow after the operator acts, one at a time, with source `console`; the relay re-validates every emission through the intent path exactly as it does for a button press. The compiled-plan audit record (`plan_compiled`) lands in the session log without the transcript.

Langfuse telemetry starts only when both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are configured. It records opaque correlation and session identifiers, content type, byte count, model, and outcome. Audio and transcript text stay out of telemetry.

Run loopback by default. An intentional LAN deployment should add its transport protection and network boundary outside the app:

```bash
uv sync --locked
uv run uvicorn relay.app:app --host 127.0.0.1 --port 8000
```

`relay.app:app` is the standalone relay: with no planner/arbiter consumer configured it refuses every intent with `downstream_unavailable`. `relay.main` composes the relay with the planner, arbiter, and the adapters `SWEEP_ADAPTER_BACKEND` selects (see "Autonomy composition" below). It additionally reads `SWEEP_PLANNING_JSON` and `SWEEP_SAFETY_JSON`, plus `SWEEP_SIM_CAMERA_JSON` on the `sim` backend, each a JSON object with exactly that config's fields; `.env.example` carries the CI fixture values as demo values. `just relay` reads `.env` and runs it:

```bash
just relay        # uv run --env-file .env python -m relay.main --host 127.0.0.1 --port 8000
just fake-node    # another terminal; with SWEEP_ADAPTER_BACKEND=remote the console drives it
```

## Authentication and connection binding

Connect to `/ws/{session_id}`. The first and only unauthenticated frame is one of:

```json
{"v":1,"type":"auth","source":"console","token":"..."}
{"v":1,"type":"auth","source":"keyboard","token":"..."}
{"v":1,"type":"auth","source":"webcam","token":"..."}
{"v":1,"type":"auth","source":"adapter","drone_id":1,"token":"..."}
```

The first successful server event is `auth.accepted`; the browser must not mark itself connected or authenticated before it receives this event. It is followed by the current state.

```json
{"v":1,"t":1756700000000,"type":"auth.accepted","event_id":"...","session":"demo","source":"console","drone_id":null}
```

`auth.refused` contains `event_id`, `session`, `status: "refused"`, and machine-readable `reason` plus display-only `detail`, then the server closes with policy code 1008. Auth frames, credentials, and signatures are never written to the audit log. After authentication, an Intent v1 `source` must exactly equal the bound `console`, `keyboard`, or `webcam` source. An adapter connection is bound to one configured `drone_id`, and a second live connection for that ID is refused.

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

Coordinated dispatch creates a durable operation marker for every delivered group member before adapter I/O. The relay commits each member's outcome and includes sibling lifecycle evidence in the coordinator's response, so a sibling worker can retrieve its result without repeating adapter work. An interruption before those outcomes commit leaves replay fail-closed.

Undelivered stop reservations preserve executable recovery actions and the conflict HOLD. Completed coordination retains timestamp history for the intent freshness window, allowed future-clock skew, and conflict window, and longer while a related admitted request awaits delivery. Takeoff, translation and capture within the stop’s conflict window remain superseded. The stop-history rule allows newer come-home and fleet landing requests immediately. Requests dated at or before that stop remain superseded, and late members of a motion conflict remain refused. Newly issued motion outside the conflict window remains executable.

## Membership and state fan-out

Each state snapshot carries a session-local, increasing `state_sequence`. Consumers use it to order the full projection across sockets, including snapshots generated in the same millisecond. Lifecycle acknowledgements remain deliverable when a newer roster makes an accompanying projection stale.

The console ignores membership projections older than its current roster or already covered by an authoritative state snapshot. A delayed membership frame cannot undo aircraft readiness, selection, or a preview built against the newer roster.

Every accepted membership transition is immediately followed, in the same ordered publication, by a `state` event. Membership values are exactly `registered`, `ready`, `leaving`, `disconnected`, and `degraded`. A session retains records and membership history for disconnected aircraft, caps physical stable IDs at four, increments `connection_epoch` on rejoin, and increments `roster_version` on membership changes. Join and rejoin do not modify the current selection or accepted plan.

`graceful_leave` defaults closed. Integration must provide `leave_authorizer_factory` to `create_app`; its per-session callback receives `(drone_id, connection_epoch, current_state)` and returns true only after the autonomy path proves landed, disarmed, and task-free. Without that approval, the relay emits `graceful_leave_not_authorized`. After approval, the registry atomically removes the aircraft from selection and clears pending confirmation and the accepted prior-roster plan while entering `leaving`. That membership event is followed by a one-shot state carrying `invalidated_intent_ids`, `invalidation_reason: "graceful_leave_roster_change"`, `prior_roster_version`, and `cleared_control_fields`; periodic states do not repeat this transition metadata. A socket closing without an authorized leave is recorded as unexpected loss.

State is fanned out at 10 Hz. Its required top-level keys are:

```text
v, t, type="state", event_id, session, roster_version, armed, estop,
selection, formation, spacing, mode, capability_profile, enabled_intent_names,
pending, accepted_plan, drones
```

Each drone has these required keys:

```text
drone_id, connection_epoch, membership, readiness_reasons, flight_state,
battery, link, pos_quality, control_authority, last_seen_at, camera_patterns,
selectable, adapter_id, adapter_capabilities, home_pose,
rc_safety_operator_present, telemetry, membership_history,
camera_capabilities, node_status
```

`flight_state`, battery/link/position aliases, and `last_seen_at` are a normalized console projection and are nullable until current telemetry exists. The nested `telemetry` object is the authoritative Appendix B snapshot; its transport-only event ID, session, and connection epoch are represented by the containing drone/event. `camera_patterns` is derived from the signed capability list and does not assert camera readiness. The relay does not invent storage, camera-ready, active-task, or operator-presence/timing facts absent from Appendix B. The autonomy boundary must enrich those inputs explicitly and fail closed when they are missing.

`camera_capabilities` and `node_status` are the node's latest `capabilities` and `node_status` frames (see the node protocol below) without their transport-only fields, or null until the node has sent one in the current connection epoch; a rejoin clears both. They are informational projections for the console and the command wire. Neither changes membership or `control_authority`: only a signed `readiness` frame does that, so a node that loses authority must report it through readiness as well as `node_status`.

Top-level `armed` is the authoritative session arm authorization, initially false and updated only through `RelaySession.update_control_projection(armed=...)` after the planner/arbiter accepts that control-state change. It is not inferred from aircraft flight-state strings. Join and rejoin leave it unchanged; a new session after process restart begins disarmed. Per-aircraft physical armed/disarmed evidence remains an explicit autonomy enrichment used by graceful-removal safety.

The capability profile limits valid intent names before planning. Every non-null intent sink must expose the same immutable `CapabilityProfile` as the relay session; opaque callbacks must be wrapped in `CapabilityBoundIntentSink`. The session revalidates that declaration before admission and again before pending execution, so replacing a planner behind a bound method cannot silently widen the command surface. A capability profile does not approve an adapter or aircraft deployment. Authenticated membership, adapter capabilities, current telemetry, control authority, RC-safety-operator presence, and the planner and arbiter gates remain required before hardware dispatch.

Server WebSocket event types are `auth.accepted`, `auth.refused`, `membership`, `state`, `telemetry`, `safety_action`, `acknowledgement`, `refusal`, and the node-authored `capabilities`, `capture_readiness`, and `node_status`; a node's socket additionally receives `command` and `control_heartbeat`. Every one carries `event_id`. Node-local `safety_action` events expose the aircraft, connection epoch, and HOLD or FAILSAFE action so operators can see link-loss intervention. A refusal always includes all of `intent_id`, `command_id`, `drone_id`, `connection_epoch`, `roster_version`, `reason`, and `detail`; context fields are deliberately present as null when they do not apply. Acknowledgements use the same always-present context fields; `command_id` is non-null for adapter facts and nullable for relay/orchestrator intent-level lifecycle events.

## Node protocol

A bridge node (the phone app, or `adapters.dji_mini3.fake_node` before hardware exists) is an adapter connection. It authenticates with its drone ID and per-aircraft key, sends the signed `join` and `readiness` frames above, streams telemetry, and then speaks the command wire described here. `adapters/README.md` describes the relay-side `RemoteBridgeAdapter` that drives it.

`auth.accepted` carries a `node` object for adapter connections and null for consoles. It distributes the relay-configured thresholds so no node invents its own:

```json
{"v":1,"t":1756700000000,"type":"auth.accepted","event_id":"...","session":"demo","source":"adapter","drone_id":1,"node":{"command_ttl_ms":2000,"virtual_stick_hz":10,"watchdog_hold_ms":2000,"watchdog_failsafe_ms":10000}}
```

```dotenv
SWEEP_ADAPTER_BACKEND=sim
SWEEP_COMMAND_TTL_MS=2000
SWEEP_COMMAND_DEADLINE_MS=10000
SWEEP_VIRTUAL_STICK_HZ=10
SWEEP_NODE_WATCHDOG_HOLD_MS=2000
SWEEP_NODE_WATCHDOG_FAILSAFE_MS=10000
```

`SWEEP_ADAPTER_BACKEND` selects which adapters `relay.bridge.build_adapters` and `build_dispatcher` construct for a session: `sim` (the deterministic simulator, with an explicit `SimCameraConfig`) or `remote` (one `RemoteBridgeAdapter` over the bridge wire, bounded by `SWEEP_COMMAND_TTL_MS`). The relay itself never dispatches; `relay.autonomy`, the composition `relay.main` runs, calls that factory for each accepted intent. `SWEEP_COMMAND_DEADLINE_MS` bounds one command's total wait regardless of non-terminal progress acknowledgements and must be at least the TTL; at the deadline the adapter returns the last non-terminal acknowledgement and the plan reports `executing`. `SWEEP_VIRTUAL_STICK_HZ` must stay within the documented 5 to 25, and the watchdog values must satisfy `0 <= hold < failsafe`. These are demo values; measure and configure them for a hardware session.

### Control heartbeat (relay to one node)

Once an authenticated adapter has joined, the relay sends it a per-connection control
lease once per second by default, or twice per configured hold window when that window is
shorter than two seconds. It is routed only to that adapter, never broadcast to consoles,
and is not an audit-history event:

```json
{"v":1,"t":1756700000000,"type":"control_heartbeat","event_id":"...","session":"demo","source":"relay","drone_id":1,"connection_epoch":2,"roster_version":4,"seq":7,"signature":"..."}
```

The signature is HMAC-SHA256 with that adapter's key over the exact frame without
`signature`. `seq` starts at one for each authenticated socket and strictly increases. A node
refreshes its deadman only after the signature, session, drone id, current connection epoch,
current roster version, timestamp freshness, and sequence all validate. Commands, state,
membership, acknowledgements, parseable telemetry, and fan-out echoes are deliberately not
liveness evidence.

### Command frame (relay to node)

```json
{"v":1,"t":1756700000000,"type":"command","event_id":"...","session":"demo","command_id":"...","intent_id":"intent-1","roster_version":4,"drone_id":1,"connection_epoch":2,"seq":7,"issued_at":1756700000000,"ttl_ms":2000,"operation":"goto","args":{"x_mm":1200,"y_mm":-400,"z_mm":1000,"speed_mm_s":500},"signature":"..."}
```

- `operation` is the planner operation set: `takeoff`, `goto`, `rotate_to`, `hover`, `land`, `estop`, `camera_capabilities`, `set_gimbal_pitch`, `camera_ready`, `capture_panorama`, `capture_photo`, `retrieve_media`.
- `args` is exact per operation and contains integers and IDs only: `takeoff` `{z_mm}`; `goto` `{x_mm, y_mm, z_mm, speed_mm_s}`; `rotate_to` `{yaw_mdeg, speed_mdeg_s}`; `set_gimbal_pitch` `{pitch_mdeg}`; `capture_panorama` and `capture_photo` `{capture_id}`; `retrieve_media` `{file_id}`; every other operation `{}`. Speeds are positive. Integer millimetre and millidegree units keep the signed canonical JSON free of floats, the same rule signed membership claims follow.
- `signature` is the HMAC-SHA256 described above, computed with that aircraft's configured key over the frame with `signature` omitted, so the node can verify the relay authored the command. `relay.contracts.command_event` builds the unsigned frame and `relay.auth.sign_event` signs it.
- `seq` is monotonic per node per connection epoch, starting at 1. `issued_at` plus `ttl_ms` is the local deadline. The node admits a command only when its signature verifies, `connection_epoch` is the node's current epoch, `roster_version` matches the last state it received, the deadline has not passed, and `seq` exceeds the last admitted `seq`; otherwise it acknowledges `failed` with `stale_command` or `out_of_order_command` and never resends.
- The relay audits every issued command without its signature and delivers the signed frame only to the socket bound to that drone; consoles do not receive commands. Dispatcher-owned calls preserve the planner's `command_id` on the wire, giving late results one exact end-to-end identity; direct diagnostic adapter calls generate a unique ID. A command whose `intent_id` the session has not seen (an autonomy-originated safety plan) registers that intent as executing so the node's acknowledgements correlate; a terminal intent cannot receive new commands.

The node answers with the adapter acknowledgement frame above, echoing `intent_id`, `command_id`, `drone_id`, `connection_epoch`, and `roster_version`: `accepted` on admission, `executing` when work starts, then `completed` or `failed`. A failure names one of the machine-readable reasons the node may return, `stale_command`, `out_of_order_command`, `authority_lost`, `watchdog_hold`, and `watchdog_failsafe`, or another snake_case reason. `RelaySession.await_command_acknowledgement` hands each acknowledgement to the waiting remote adapter and releases the wait after a terminal status or a timeout; later acknowledgements remain audited facts.

### Node-authored frames

All node-authored frames carry `drone_id` and `connection_epoch`, rely on the authenticated drone binding like telemetry, and pass the same session, freshness, replay, ordering, and current-epoch checks. They are not signed.

- `capabilities`: the `CameraCapabilities` fields (`native_panorama_modes`, `photo_capture`, `gimbal_pitch_min_deg`, `gimbal_pitch_max_deg`, `horizontal_fov_deg`, `storage_remaining_bytes`, `media_retrieval`) plus the probed hardware profile (`aircraft_model`, `aircraft_firmware`, `rc_firmware`, `phone_model`, `android_version`, `sdk_version`, nullable `measured_hfov_deg`). Fanned out to consoles and projected into the drone's `camera_capabilities`.
- `node_status`: `virtual_stick_enabled`, `control_authority`, nullable snake_case `authority_change_reason`, `watchdog_state` (`nominal`, `hold`, `failsafe`), `video_publish_state` (`stopped`, `connecting`, `publishing`, `failed`), `phone_battery_percent`, and `phone_thermal_state` (`none`, `light`, `moderate`, `severe`, `critical`, `emergency`, `shutdown`). Fanned out and projected into the drone's `node_status`; informational only.
- `capture_readiness`: nullable `room_id` and `capture_id`, `guidance_mode` (`visual_advisory` or `registered_metric`), `pose_source`, the `pose_ok`, `clearance_ok`, `camera_ok`, `storage_ok`, `motion_ok`, and `image_quality_ok` gates, `coverage_missing` azimuths in degrees, nullable `next_heading_deg`, and nullable `suggested_delta` `{kind: yaw|gimbal, degrees}`. Fanned out unchanged and not projected into state; the session retains the latest frame per aircraft (`RelaySession.capture_readiness`) as the autonomy boundary's camera-readiness evidence.
- `media_file`: the `MediaFile` fields (`capture_id`, `file_id`, `timestamp_ms`, `drone_id`, `connection_epoch`, `pose`, `actual_yaw_deg`, `gimbal_pitch_deg`, `intrinsics`, 64-character lowercase hex `checksum_sha256`, `storage_ref`, `retrieval_status`). Audited and retained for the command wire, not fanned out. A node sends the `media_file` before the terminal acknowledgement of the capture or retrieval command that produced it.
- `capture_bundle`: `room_id`, `capture_id`, `pattern`, `coverage`, `status`, nested `media` records, and nullable `reason` and `detail`; a `failed` or `unsupported` bundle requires a machine-readable reason. Audited and retained, not fanned out.

The fake node runs against a live relay with `just fake-node` or `uv run python -m adapters.dji_mini3.fake_node --drone-id 1`; it reads its credential from `--token`, `SWEEP_ADAPTER_KEYS_JSON`, or `SWEEP_RELAY_TOKEN`. `relay/tests/test_bridge_roundtrip.py` starts the relay in-process on the `remote` backend, connects the fake node, and dispatches a safety hold through `build_dispatcher` and the remote adapter end to end.

## Autonomy composition

`relay.autonomy` is the planner/arbiter consumer behind `create_app`'s `intent_sink_factory` and `leave_authorizer_factory`; `relay.main` builds it with `create_autonomy_app`. Each session runs three worker lanes (below). The sink only queues, because the session calls it inside the intent operation and a remote dispatch must wait for node acknowledgements that arrive through that same session. A worker builds the `FleetSnapshot` from `current_state()`, calls `relay.bridge.build_dispatcher` for that snapshot, and runs `planner.controller.AutonomyController` (capability gate, `check_intent`, plan, `check_plan`, per-command `check_command`, dispatch). It then applies the plan's explicit `armed_update` and `selection_update` through `update_control_projection`, only while the plan's roster is still the session's roster (otherwise the result becomes `invalidated` with `stale_roster`), publishes a plan still waiting on a node's terminal acknowledgement as `accepted_plan` and clears it on every terminal result, and reports the result with `record_lifecycle` under `source: "autonomy"` and a null `command_id`; node acknowledgements keep `source: "adapter"` and their `command_id`. Graceful leave is authorized through `planner.roster.authorize_graceful_removal` on the same snapshot.

Appendix B carries no physical armed, physical-RC, storage, camera-readiness, active-task, position-loss, or Sweep-operator facts, so `relay_snapshot` asserts them explicitly and fails closed: operator presence and activity come from the latest accepted console or keyboard intent in the session (the arbiter's operator timeout bounds it; no intent yet means no operator); physical armed evidence is derived from the authoritative telemetry flight state exactly as the simulator reports it (every state except `disarmed` and `landed`), so the arbiter's physical-armed gate cannot refuse anything its flight-state gates do not already refuse, an accepted limitation until a node reports a motor-armed fact; physical-RC availability is the same signed `rc_safety_operator_present` readiness claim, because the wire carries no separate RC-link fact, so the arbiter's two RC gates are intentionally collapsed into one at this stage and must not be read as defence in depth; storage comes from the node's current-epoch `capabilities` frame (none means zero) and camera readiness requires the node's latest current-epoch `capture_readiness` frame to report both `camera_ok` and `storage_ok` (no frame means not ready); `active_task_id` is null because intents run one at a time; `position_loss_since_ms` is null so the controller's dwell falls back to the position timestamp. Aircraft without current-epoch telemetry are excluded from the snapshot and cannot be selected or commanded until their node reports.

On `remote`, every planned command becomes a signed `command` frame to the node bound to that aircraft and the node's acknowledgements complete it. If a nonterminal result is followed by a timeout, the exact issued-command ledger retains the execution owner for a bounded interval and a later terminal result resumes the plan without resending the completed command. On `sim`, the dispatcher runs the in-process simulator built from the snapshot; a live relay has no telemetry source for it, so the registry stays as the nodes report it and `sim` remains the CI backend. Not wired yet: the positioning-loss monitor (`handle_positioning_loss`) and motion-conflict pairing (`execute_pair`); the physical RC remains the independent stop path.

The three lanes give stops priority. `estop` runs at once on its own lane and cancels whatever the other two lanes are executing; `hold` runs at once on a second lane and cancels a running operator motion or camera plan (`takeoff`, `translate`, `come_home`, `capture_room`), but queues behind a running `land_all` or `estop` rather than interrupting a safety plan; everything else runs in arrival order on the third lane. Both stops also cancel queued motion and camera intents. The network stop latches from the intent, never from its plan: the sink sets the session's `estop` inside the operation that accepted the intent, before any dispatch, and marks every later snapshot `estop_active`, so no worker, plan, publish, or session-lookup failure can lose the latch and an operator intent that starts in between is refused rather than sent. Cancellation is atomic with the wire: inside the stop's own intent operation, under the session lock, the sink records the cancelled intent as `invalidated` with reason `preempted_by_estop` or `preempted_by_hold`, so `issue_command` refuses anything that plan tries to send afterwards; the plan's dispatch also checks its flag before every command, before every send, and after every acknowledgement wait, and exits without a best-effort hold because the stop is the safety action. A cancelled plan therefore exits at its next acknowledgement or after at most one command TTL of silence, and every command is bounded in total by `SWEEP_COMMAND_DEADLINE_MS`. `RemoteBridgeAdapter.estop` sends to every aircraft before waiting on any acknowledgement, so a node that stays silent fails only its own result (`adapter_timeout`) while the stop still latches. `relay/tests/test_autonomy_roundtrip.py` runs the M2.0 workflow from console intents through the composition to two fake nodes.

## Audit and replay

Each normalized event is one append-only JSONL record shaped as `{"seq": N, "event": {...}}`, with a contiguous per-session sequence. Session names are SHA-256 hashed for filenames under `SWEEP_SESSION_LOG_DIR`. Any attempt to log a token, signature, authorization value, credential, password, or secret is rejected recursively.

Events from one relay operation are committed as a single audit batch. A per-session SQLite database in WAL mode records the pending operation before irreversible work begins and makes its events visible only when the whole operation completes. JSONL remains the public replay mirror with the same per-event record shape. An incomplete operation fences replay across restart.

Control projection updates record their pending operation before changing any field. If copying a later field fails, the session rejects further mutations, state reads, and replay, including when a planner callback catches the original exception.

Live appends compare the mirror's file identity, size, and modification metadata with the last verified append. An unchanged mirror requires no history reads; a changed mirror receives full validation. Reopen and replay always verify the complete history.

Authenticate HTTP requests with `Authorization: Bearer $SWEEP_RELAY_TOKEN`. `GET /metrics` returns relay/session counters. `GET /session/{id}?after_sequence=N` returns a `replay` envelope whose `events` are the ordered JSONL records after `N`. `intent_record` is log-only and pairs the normalized Intent v1 request with its accepted/refused outcome; membership, telemetry, state, acknowledgement, and refusal records use the same event shapes delivered live. Replay UI remains outside M2.0.

A live session ID is scoped to one relay process lifetime. After restart, any ID whose persisted log is nonempty is replay-only: a correctly authenticated WebSocket receives `auth.refused` with `reason: "session_closed"` and must reconnect under a new session ID. The replay endpoint reads that closed log without constructing mutable fleet state. This deliberately prevents a restarted relay from appending new roster versions or connection epochs starting at one; safe live-state restoration also requires autonomy-owned plan/confirmation invalidation and loss handling and is not part of M1.1.

On first reopen of a legacy JSONL log, the relay removes only a nonempty, unterminated EOF fragment after validating every complete record, then imports that prefix into the transaction database. Later recovery verifies the JSONL mirror against completed database operations. Complete malformed records and divergent mirrors fail closed. A repaired log remains evidence of prior session use even when its first record was torn, so that session ID stays replay-only.

Controller-generated safety stops reserve the `safety:` intent ID prefix. Public requests using that prefix are refused without occupying the intent ledger.
