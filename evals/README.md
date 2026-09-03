# evals

Capability area: Platform. Milestone: M1 onward.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes that encode shared-contract or safety expectations name one change owner and require cross-review.

Four gold sets (PRD section 4.7):

1. Gesture: recorded webcam sessions with hand-labeled intent timestamps.
2. Language: 200 utterances with gold intent sequences.
3. Simulator scenarios: ten scripted missions with pass/fail assertions on final state and safety log.
4. Hardware acceptance: the scripted mission on real drones, five consecutive passes before any demo.

Sets 1 to 3 run in CI on every merge. Every bug becomes a scenario or a gold-set item before it is fixed.

`language_corpus.py` loads the versioned language cases and grades the production compiler's ordered semantic intents or typed refusal. Synthetic responses bootstrap CI until the reviewed corpus and provider recordings land. The replay transport fails on a missing recording and never falls through to a network call.

Eval runs can append a manifest and per-case model usage to JSONL, then render a static HTML dashboard with pass rate, outcome, usage, and latency. Gold outcomes remain separate from provider recordings.
