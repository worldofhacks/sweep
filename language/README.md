# language

Owners: A (front end) and C (LLM plumbing); B writes the resolvers. Phase 5.

Plan compiler: swarm state plus intent schema plus utterance in, an ordered plan of intents out through schema-constrained output. `validate_plan` runs, the console previews, the operator confirms, and intents are emitted one at a time through the relay. Selection and location resolvers, prompts, and a local-model fallback live here. Safety rules live in the arbiter, not in the prompt.

PRD: sections 4.3, 4.4, 4.5, 5.10, 8.4.
