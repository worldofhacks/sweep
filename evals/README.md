# evals

Capability area: Platform. Milestone: M1 onward.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes that encode shared-contract or safety expectations name one change owner and require cross-review.

Four gold sets (PRD section 4.7):

1. Gesture: recorded webcam sessions with hand-labeled intent timestamps.
2. Language: 200 utterances with gold intent sequences.
3. Simulator scenarios: ten scripted missions with pass/fail assertions on final state and safety log.
4. Hardware acceptance: the scripted mission on real drones, five consecutive passes before any demo.

Sets 1 to 3 run in CI on every merge. Every bug becomes a scenario or a gold-set item before it is fixed.

`language_corpus.py` loads the versioned language cases and grades the production compiler's ordered semantic intents or typed refusal. Synthetic responses bootstrap CI until the reviewed corpus and provider recordings land. The replay transport fails on a missing recording and never falls through to a network call. Each response records both how it was obtained (`anthropic`, `replay`, or `synthetic`) and whether its contents originated from Anthropic or a synthetic fixture.

Replay results use `unverified_replay` provenance because an editable cassette cannot independently prove its provider origin. Provider acceptance and corpus recordings remain release evidence that must be captured from the live API. Wrap `AnthropicTransport` in `RecordingTransport` to grade and record the same responses in one run; each result digest resolves to `<cassette filename>.snapshots/<digest>.json` beside the growing cassette.

Selected-aircraft `land` preserves the current selection through the compiler, relay, planner, and adapter. The synthetic replay passes all 53 cases through the production compiler. Reviewed live-provider evidence remains outstanding; synthetic responses and mocked recording tests cover the deterministic path.

Eval runs can append a manifest and per-case evidence to JSONL, then render a static HTML dashboard with pass rate, outcome, usage, and latency. The default corpus is an immutable, digest-pinned 53-case release. A run requires every reviewed case exactly once in corpus order and rejects results whose IDs, metadata, or digest differ. Synthetic response IDs must match that same order. The manifest records those case IDs, the model, prompt schema, corpus digest, cassette digests, response provenance, category coverage, and live-demo count. Gold outcomes remain separate from provider recordings; host-created capture IDs use an explicit sentinel rather than being silently substituted during grading.

### Speech provider comparison

Run the same 20 recorded clips against both configured providers:

```sh
uv run python -m evals.voice_provider_benchmark path/to/smoke.json --output .sweep/voice-comparison
uv run python -m evals.voice_provider_benchmark path/to/smoke.json --output .sweep/voice-comparison --replay
```

The manifest is a JSON array of 20 objects with `id`, `audio` (a path relative to
that manifest), `content_type`, and the reference `transcript`. Every clip must
decode and fit the relay's 8 MiB and 30-second limits. Both provider keys must be
configured for a complete live comparison. The runner records provider cassettes,
per-clip audio hashes, word errors, latency, and aggregate word accuracy plus
median/p95 latency. Accuracy ignores casing and punctuation and counts word
insertions, deletions, and substitutions. Failed calls count as missing words.
Replay uses the same audio and cassettes without reporting local lookup time as
provider latency. Retain the recordings with the results so another operator can
reproduce the comparison. The existing transcript-only fixtures are synthetic and
cannot supply the recorded-audio smoke measurement.
