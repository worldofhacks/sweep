# relay

Capability area: Platform. Milestone: M1.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to shared contracts or the authoritative relay state shape name one change owner and require cross-review.

The intent bus. FastAPI with a WebSocket endpoint that accepts intents from any source, authenticates them with the shared token, stamps and logs them to append-only JSONL, forwards them to the planner, holds the authoritative swarm state, and fans state and telemetry out to consoles at 10 Hz. Exposes `/metrics` and `/session/<id>` for replay. Single process; restart-safe because state is rebuilt from adapter telemetry.

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
- `intent_id` is a non-empty stable identifier. A retry gets a new identifier and may link to a different failed request through `retry_of`.
- `confirm` records the source's confirmation state. `capture_room` requires confirmation and exactly one selected drone; the arbiter enforces the remaining action-specific checks.
- Rejection precedence is envelope, registered source, intent name, argument shape, then M2.0 capability.
- M2.0 accepts the eight flight-control names plus the previously accepted `capture_room` path. The remaining names keep their v1 argument shapes and return `unsupported`.
- `come_home` returns selected drones to their home positions through planner-generated `goto` calls. The separate confirmed `land_all` intent maps to adapter `land`.

The current source registry is `console` and `keyboard`. Language and webcam join only when their real producers and conformance tests land. Registering another source or enabling another Intent v1 name changes the shared constants and conformance tests in this module.
