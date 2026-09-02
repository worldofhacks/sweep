# Scope-creep / overengineering review of scaffolding-review-round1-dev.md

## Verdict up front

The draft holds up well against Koby's stated fear. Four of five tickets (A1–A3, B1) are
ready to become GitHub issues close to as-written. B2 has one real instance of the exact
trap Koby named — generalizing an interface for a vehicle type that doesn't exist yet —
and needs one cut before filing. A1 has a labeling error, not a scope error: part of what
it calls "scaffolding-only, adds no ... behavior" is in fact new relay runtime behavior.
The #3 narrowing recommendation is clean and matches Koby's own words; I'd only tighten
the title further.

I pulled the three live GitHub issues (`worldofhacks/sweep` #1, #2, #3) directly via the
API rather than trusting the draft's summary of them, and checked the repo tree. Both
check out: #1 is exactly the 5-field, B-owned, no-cross-team-dependency ticket the draft
describes; #3 already carries the "explicitly rejected" list the draft assumes; the repo
genuinely has no `console/` directory or HTML artifact anywhere, confirming A2's premise.

## 1. Per-ticket scope-creep check

### A1 — Intent v1 + conformance suite: no creep, one mislabel

Nothing in A1 is generality built for a hypothetical future. The source registry exists
specifically because the brief requires a new source (`band`, already named) to register
**without touching relay code** (round1 dev, line 9 / A1 problem statement, line 40). A
hardcoded enum would fail that constraint on the first new source — the registry is the
minimal mechanism that satisfies a concrete, already-stated near-term requirement, not a
plugin framework built "so future X doesn't need a redesign." Same for the producer-runner
contract (JS test runner for browser sources, pytest for Python sources): that's describing
two sources that exist today, not a general polyglot test framework.

**Mislabel, not scope creep (task item 2):** A1's scope classification (line 46) puts the
registry under "Scaffolding-only... These add no commands or planner behavior." But the
acceptance criteria two paragraphs later say the opposite: "Relay admission also rejects a
well-formed source identifier that is absent from the registry" (line 59–60). That's a new
runtime gate in the relay's intent-admission path — not a command or planner change, but
it is new relay *behavior*, which contradicts the "adds no ... behavior" framing even though
it correctly avoids planner/arbiter changes. Low risk either way (C already owns relay
schema work this phase), but the ticket should say "current capstone path: relay admission
gate keyed off the registry" rather than filing it under scaffolding-only. Fix the label,
don't cut the mechanism.

**Worth trimming, not blocking:** the acceptance criterion "A registry test rejects
duplicate IDs, invalid IDs, and entries without a conformance runner" (line 60) asks for a
small validation framework around a list that will hold four or five entries, added only by
the three engineers themselves. One CI assertion ("every registry entry has a runner") gets
the actual protection; duplicate-ID and invalid-ID checks are defending against a failure
mode (an adversarial or careless external contributor) that doesn't exist yet for a
three-person capstone team. Cut those two checks from the acceptance criteria; keep the
"every entry needs a runner" one.

### A2 — restore Phase 0 webcam: no creep

Straight Phase 1 work, already on the PRD's critical path (§6, §8.3). The "pure boundary"
between recognition and intent construction (line 91) is the minimum needed for A1's
conformance suite to test real producer code instead of a fixture — not extra generality,
it's what makes A1's stated non-negotiable ("a test that reads expected JSON and validates
that same JSON does not satisfy this issue," line 63) actually true for this source. The
stop condition (line 105–107: halt and open a separate ticket if the Phase 0 artifact can't
be found) is the right call, not scope creep in either direction — confirmed the artifact
is in fact absent from the repo tree.

### A3 — register glasses and band: no creep

Band is Koby's own named example of "other forms of intent communication." Nothing here
adds a device abstraction beyond registering two more sources through A1's mechanism; the
ticket explicitly shares implementation code between glasses and band only where the SDK
forces it, and keeps distinct source IDs for audit purposes (line 135) rather than
inventing a shared device class hierarchy. Non-goals correctly exclude per-device intent
sets and capability negotiation (line 142–144).

### B1 — capability-profile execution oracle: no creep, correctly scoped to #1

This is the cleanest ticket in the set. It consumes exactly #1's 5-field descriptor
(confirmed against the live issue #1 body — same 5 fields, same B ownership, same
"explicitly excluded" camera/gimbal/gps/formation list) and adds nothing to it. The checks
it runs (line 178–183: indoor positioning, position_control, speed ceiling, Appendix-C-only
calls) are all direct consequences of #1's existing 5 fields — no invented capability
concept. "No production capability branching is added solely for this test" (line 194) is
an explicit, correct guard against the oracle leaking into production adapter dispatch, and
the scope classification's "scaffolding-only" label is accurate here — the check really
does live only in the eval/test harness, not the runtime call path (unlike A1's registry
gate).

### B2 — capability-aware model benchmark: one real cut needed

This is where the draft comes closest to the trap. Two places generalize for a vehicle type
that has no adapter, no intent schema, and no committed design:

1. **"Intent schema ID" as a runner parameter** (scope classification, line 219: "make the
   runner accept an intent-schema ID, capability-profile fixture, compiler callable, and
   deterministic execution oracle") and **"Intent schema version" as a required dataset-case
   field** (line 227). There is exactly one intent schema — Appendix A, drone — and no second
   one is scoped anywhere in the PRD or in issue #3 (rover/boat/RC-car adapters are explicitly
   gated behind "a proven versioned capability contract" that doesn't exist yet). Parameterizing
   the harness over a dimension with one legal value today is the textbook case Koby is
   worried about: an interface shaped for a hypothetical second value instead of the one that
   exists. **Cut:** drop "intent-schema ID" from the runner signature and "Intent schema
   version" from the dataset-case shape. Hardcode Intent v1. Reintroduce the parameter if and
   when a second vehicle's intent schema is actually specified — at that point you'll know its
   real shape instead of guessing at one now.

2. Everything else that gestures at multi-vehicle support is correctly kept as an
   *assertion about the harness's shape*, not built work: "Adding a future vehicle benchmark
   requires a profile fixture, a reviewed vehicle validator, and dataset cases. The harness
   core and report schema remain unchanged" (line 255) is a design claim to hold the team to
   later, not a piece of code being asked for now. That's the right way to answer "does it
   generalize" without writing the generalization. Same for the synthetic-limited-profile
   cases (line 252) — they test B1's ability to say "unsupported," using a hypothetical
   profile, but they add zero runtime capability and the ticket says so explicitly. Keep both.

With the intent-schema-ID cut, B2's remaining scope (compare LLM compiler vs. local router
from #2, on the frozen drone intent set, scored through B1) is squarely "current capstone
path" work already implied by PRD §4.7 and issue #2, not new generality.

## 2. Scaffolding-only claims that actually add runtime behavior — summary

Checked every "Scaffolding-only" line in the draft against its own acceptance criteria:

| Ticket | Claim | Actually scaffolding-only? |
|---|---|---|
| A1 | registry, shared cases, runner contract, guide (line 46) | **No** — the relay admission gate (line 59–60) is new relay runtime behavior. Correctly kept off the planner/arbiter, but shouldn't be filed as "adds no ... behavior." |
| A2 | pure emitter boundary + first runner (line 91) | Yes — refactor + test only, no new production path beyond what Phase 1 already requires (wiring the console to the relay). |
| A3 | two registry entries + runners (line 128) | Yes — same registry mechanism as A1, no new device behavior. |
| B1 | typed execution oracle (line 171) | Yes — explicitly confined to the eval harness, with a non-goal barring production branching (line 194). |
| B2 | profile-aware runner (line 219) | Yes for the runner itself; **no** for the intent-schema-ID axis, which isn't scaffolding for anything real yet (see above) — it's speculative surface, not behavior, but it's the same over-generalization risk in interface form rather than runtime form. |

Only A1's mislabel rises to "this claim is wrong." B2's issue is better described as
premature interface generality than a mislabeled runtime claim.

## 3. Issue #3 narrowing — clean

Compared the draft's recommendation against the live issue #3 body. The draft proposes
keeping adapter-capability-contract-v2, additional vehicle adapters (gated on that
contract), and post-Phase-6 B1/B2 extensions, while dropping the primitive-representation,
formation/sweep migration, semantic world-state, grounded-entity-resolver, camera/gimbal-
boundary, and inspect/search tickets (draft lines 293–299). That drop list matches the live
issue's tickets 3–8 exactly, and the keep list matches tickets 1–2. What survives is vehicle
*adapter portability* — the literal thing Koby asked for ("ability to add rovers, boats, RC
cars, etc") — with nothing about a universal command layer, world model, or new intents. It
doesn't smell like Jarvis; it smells like "add more vehicles later, safely." One tightening:
rename to something more specific than the draft's own suggested title implies — "Epic:
post-Phase-6 vehicle adapter portability" is good, but I'd fold "post-Phase-6 extensions to
B1/B2" into that same sentence explicitly (e.g., "...including extending the B1/B2 eval
harness to real rover/boat/RC-car adapters") so it can't quietly regrow into a generic
"future eval work" bucket that absorbs scope creep later under a clean-sounding name.

## 4. Ready-to-file verdict

| Ticket | Ready as-is? | Required change before filing |
|---|---|---|
| A1 | No | Re-label the relay admission gate as capstone-path, not scaffolding-only (scope classification, line 46). Trim duplicate-ID/invalid-ID registry checks from acceptance criteria (line 60); keep the missing-runner check. |
| A2 | Yes | None. |
| A3 | Yes | None. |
| B1 | Yes | None. |
| B2 | No | Drop "intent-schema ID" from the runner signature (line 219) and "Intent schema version" from the dataset-case shape (line 227). Hardcode Intent v1; add the parameter back only when a second vehicle intent schema is actually specified. |
| #3 recommendation | Yes, with a wording tweak | Fold the B1/B2 post-Phase-6 extension item explicitly into the renamed epic's scope sentence so it doesn't become a generic catch-all later. |

Nothing in A1–A3 or B1–B2 resurrects universal primitives, semantic world state, or
`inspect`/`search` under a new name — I checked every non-goals section against the
rejected-scope list and issue #3's explicit rejections, and they're consistent throughout.
The one real finding is B2's premature multi-schema parameterization; everything else is a
labeling fix, not a scope fix.
