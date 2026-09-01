# relay

Owner: C (Platform). Phase 1.

The intent bus. FastAPI with a WebSocket endpoint that accepts intents from any source, authenticates them with the shared token, stamps and logs them to append-only JSONL, forwards them to the planner, holds the authoritative swarm state, and fans state and telemetry out to consoles at 10 Hz. Exposes `/metrics` and `/session/<id>` for replay. Single process; restart-safe because state is rebuilt from adapter telemetry.

PRD: sections 4.2, 5.2, Appendix A (intent contract), Appendix B (telemetry and state fan-out).

Run from the repo root: `uv run python -m relay.<module>`.
