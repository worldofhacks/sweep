# Architecture decisions: Jarvis proposal, input/eval scaffolding, glasses removed

Decision record from the architecture review that preceded PRD v0.3. Working transcripts
(round-by-round agent reviews) aren't kept here — this is the outcome and the
reasoning, not the process. The issues linked below were later closed and folded
into `docs/mvp-plan.md`; research detail lives in `docs/prior-art-intent-mapping.md`.

## 1. Jarvis-style generalization — rejected for now

Koby proposed generalizing Sweep toward a broader "intent runtime for autonomous
vehicles": `capabilities()` on `SwarmAdapter`, a local+LLM dual language compiler, a
universal command-primitive layer, and a semantic world-grounding layer.

Two independent reviews (different models) converged: none of the four changes are
free during the capstone. The universal primitive layer and semantic world model
would compete with the planner/arbiter/hardware critical path for no
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

## 3. Glasses removed; language moved up; Band resolved

Koby cut the glasses phase from capstone scope and made natural language "the
second thing built", ahead of video/perception. Feasibility review found the full language
scope doesn't fit that early; PR #10 settled the sequence: a narrow spoken-language slice
in M1, the rest in M4, and the M2.0 two-drone checkpoint ahead of both.

The team then removed glasses entirely rather than keeping them as a Future input:
the `glasses/` directory, its Appendix D entry, and every glasses reference are gone.
The band question is resolved: Wearable Devices' Mudra Link exposes events
directly to a host process, so an EMG band is the one Future input, gated on a real-device
event through the conformance suite (`docs/prior-art-emg-band-direct-integration.md`).

## 4. Process fix

An early commit landed on local `main` before push. Moved to a branch and opened as
a PR ([#9](https://github.com/worldofhacks/sweep/pull/9)) instead of leaving it
landed. Standing rule going forward: every change is branch → push → PR, nothing
lands on `main` directly.
