# language

Capability areas: Interaction and Platform. Milestones: M1 (speech capture, relay-side Whisper transcription, one pinned compiler), M4 (resolvers, full eval, local fallback).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the plan schema, `validate_plan`, or ordered emission name one change owner and require cross-review.

Plan compiler: swarm state plus intent schema plus final transcript in, an ordered plan of intents out through schema-constrained output. `validate_plan` runs, the console previews, the operator confirms, and intents are emitted one at a time through the relay. Selection and location resolvers, prompts, and a local-model fallback live here. Safety rules live in the arbiter, not in the prompt.

PRD: sections 4.3, 4.4, 4.5, 5.10, 8.4.
