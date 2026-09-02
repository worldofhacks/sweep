# Architecture decisions: Jarvis proposal, input/eval scaffolding, glasses cut

Decision record from the 2026-09-01/02 architecture review. Working transcripts
(round-by-round agent reviews) aren't kept here — this is the outcome and the
reasoning, not the process. Full ticket detail lives in the GitHub issues linked
below; research detail lives in `docs/prior-art-intent-mapping.md`.

## 1. Jarvis-style generalization — rejected for now

Koby proposed generalizing Sweep toward a broader "intent runtime for autonomous
vehicles": `capabilities()` on `SwarmAdapter`, a local+LLM dual language compiler, a
universal command-primitive layer, and a semantic world-grounding layer.

Two independent reviews (different models) converged: none of the four changes are
free during the capstone. The universal primitive layer and semantic world model
would compete with the Sept 2-6 planner/arbiter/hardware critical path for no
scripted-mission benefit, and are deferred past Phase 6. See narrowed epic
[worldofhacks/sweep#3](https://github.com/worldofhacks/sweep/issues/3).

## 2. Input scaffolding and capability-aware eval — approved, narrower scope

Koby's actual ask was narrower than "Jarvis": (A) turn the PRD's informal "any input
source that emits the frozen intents and passes contract tests is accepted" rule into
enforced scaffolding, and (B) a harness to score model output against a vehicle's
capability profile, extensible past drones without a redesign.

Filed as five issues, reviewed for scope creep before filing (one real instance
found and cut — an unused "intent schema version" parameter with no second schema to
justify it):

- [#4](https://github.com/worldofhacks/sweep/issues/4) — Freeze Intent v1 + shared input-source conformance suite
- [#5](https://github.com/worldofhacks/sweep/issues/5) — Webcam source as first conformance runner
- [#6](https://github.com/worldofhacks/sweep/issues/6) — Register additional input-source producers against Intent v1
- [#7](https://github.com/worldofhacks/sweep/issues/7) — Capability-profile execution oracle (extends #1)
- [#8](https://github.com/worldofhacks/sweep/issues/8) — Score plan-compiler models against capability profiles (extends #1, #7, #2)

## 3. Glasses cut; language moved up; Neural Band under revision

Koby cut the glasses phase from capstone scope and made natural language "the second
thing built" — immediately after Phase 1, ahead of video/perception, instead of after
glasses. Feasibility review found the full Phase 5 language scope (compiler,
resolvers, preview/confirm UI, 200-utterance gold set) doesn't fit that early; a
vertical slice does (typed text, current-selection only, one pinned compiler, ~50-item
provisional gold set), with the rest staying in the original Phase 5 window.

**Open at time of writing:** the PRD's only description of Neural Band gestures
becoming intents lives inside the glasses client that's being cut — Meta's Band SDK
doesn't expose events outside a glasses-hosted web app. Evaluating a non-Meta EMG
band (e.g. Wearable Devices Ltd's Mudra Band) as the fix, since it would let the band
register against the webcam console directly rather than needing a glasses bridge.
The PRD diff for this resequencing is pending that answer.

## 4. Process fix

An early commit landed on local `main` before push. Moved to a branch and opened as
a PR ([#9](https://github.com/worldofhacks/sweep/pull/9)) instead of leaving it
landed. Standing rule going forward: every change is branch → push → PR, nothing
lands on `main` directly.
