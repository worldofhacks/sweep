# language

Capability areas: Interaction and Platform. Milestones: M1 (speech capture, relay-side Whisper transcription, one pinned compiler), M4 (resolvers, full eval, local fallback).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the plan schema, `validate_plan`, or ordered emission name one change owner and require cross-review.

Plan compiler: swarm state plus intent schema plus final transcript in, an ordered plan of intents out through schema-constrained output. `validate_plan` runs, the console previews, the operator confirms, and intents are emitted one at a time through the relay. Selection and location resolvers, prompts, and a local-model fallback live here. Safety rules live in the arbiter, not in the prompt.

`TranscriptCompiler` sends one pinned model a fixed instruction, the operator transcript, and an allowlisted projection of authoritative relay state. Deterministic validation rejects malformed output, unavailable drone IDs, unknown rooms, and unsupported Intent v1 names before a plan can be previewed. Provider and validation failures return typed template refusals with no executable proposal.

`ConfirmedPlan` binds the preview to state and capability versions, expires it after 30 seconds by default, and revalidates before each operator-confirmed emission. It waits for a relay outcome before exposing the next step, then rebases expected state changes while preserving the roster and capability boundary. The confirming console remains the registered relay source. The language model never produces timestamps, intent IDs, source identity, confirmation state, or adapter commands.

Compiler and confirmation construction requires an audit sink. Production uses `SessionCompilerAudit` with the relay's append-only session log; tests can use `InMemoryAuditSink`. The records contain plan and state digests, ordered semantic intents, expiry, and per-step outcomes. They exclude the transcript and provider credentials.

Model telemetry uses a lazy Langfuse client when its public and secret keys are present. Keyless runs use a no-op sink without network calls or warnings. Traces group on opaque correlation and session IDs and record model usage, latency, outcome source, and a grounded score. Tracing failures cannot interrupt compilation.

The CI corpus is `datasets/utterances/transcript_plan_cases.json`. Its initial synthetic cases exercise the pipeline while the reviewed language corpus is being written. The corpus loader accepts a path and the compiler accepts live or replay transports, so replacing the synthetic corpus and recorded provider responses does not change compiler code.

PRD: sections 4.3, 4.4, 4.5, 5.10, 8.4.
