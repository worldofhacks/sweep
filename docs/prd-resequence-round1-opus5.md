# Feasibility review: prd-resequence-round1-dev.md

## Verdict up front

The phase reorder itself is sound and the Sept 5–9 language slice is a realistic scope
cut. But the draft has one finding that should block sign-off until checked with Koby,
and two that understate real load without breaking the plan outright.

**Blocking:** the claim that the Neural Band "needs no new relay endpoint, planner
path, arbiter path, web application, or phase" (line 9) and "reuses the webcam path...
[with] no new plumbing" (line 90) is not supported by the PRD's own text, and is very
likely wrong. The PRD's *only* description of how Band gestures become intents is
inside the now-cut glasses client: "glasses web app emitting the intent set from band
gestures and head direction (A)" (`docs/prd.md:276`, Phase 4 deliverables — note the
phase is literally titled "Glasses and Neural Band," `docs/prd.md:273`). The §4.2
component table attributes pinch, D-pad, and drag — the Band's actual gesture
vocabulary — entirely to the "Glasses web app" row (`docs/prd.md:118`); there is no
separate Band component anywhere in the PRD. The scaffolding draft the resequencing
draft cites for this claim raised exactly this as an open, unresolved risk rather than
a settled fact: "If the shipped SDK exposes band events only through the glasses app,
both producers may share implementation code but retain distinct source IDs"
(`docs/scaffolding-review-round1-dev.md:135`). The resequencing draft resolves that
open condition in the optimistic direction with no new evidence. That's the finding
task item 5 asked me to check, and it's real.

**Understated, not blocking:** C carries the language compiler build and the
operator-presence watchdog/session-report/recording obligations in the same week, with
the schedule only gesturing at the overlap rather than sequencing it. B's day-by-day
table quietly includes conditional Phase 5 resolver work inside Phase 2's date range,
which contradicts the draft's own Phase 2 boundary text and its "B is kept off
language entirely" framing.

## 1. Is the Sept 5–9 slice achievable by C alone, with zero help from B?

Mostly yes for the compiler/validation/logging/eval work itself — but the day table
doesn't actually keep B at zero.

The core language deliverables (compiler, `validate_plan`, ordered emission, cached CI,
50-item set) depend only on Phase 1 outputs already owned by B and delivered before
Sept 4: the frozen planner and arbiter. Building the compiler against them on Sept 5–9
requires no new B work, and the Phase 2 boundaries text says so explicitly: "Natural-
language selection and location expressions remain in Phase 5" (line 32).

But the §8.3 day table contradicts that boundary on the very days it applies to. Sept 5,
B: "Lead one-drone hardware bring-up if the delivery gate is open; **otherwise begin
bounded resolver tests**" (line 73). Sept 6, B: "Continue hardware bring-up if open;
**otherwise implement resolver success, ambiguity, and refusal cases**" (line 74). Both
are resolver work — the exact Phase 5 scope the boundaries paragraph two sections above
just said stays out of Phase 2. This isn't a scheduling conflict (it's explicitly an
"otherwise," filling B's idle time when hardware hasn't arrived, never both at once), but
it does mean the brief's own framing — "B is kept off language entirely during Sept 5-9"
— isn't what the draft actually specifies. If B ends up doing this (plausible: drone
arrival timing is uncertain per the PRD's own risk table), Phase 2 quietly consumed a
slice of B's Phase 5 resolver estimate without it showing up in Phase 2's deliverables
or exit criteria. Relabel these two cells as "early Phase 5 resolver work, time-boxed,"
not leave them looking like idle-time filler with no schedule consequence.

## 2. Day-by-day and phase-table double-booking check

Found one real instance, on C, not B.

Compare the original PRD's Sept 5 row — "Recording, session reports, operator-presence
watchdog" (C) — against the new draft. The new Sept 5 C cell replaces that entirely with
compiler work: "Define the plan schema; implement the pinned compiler against
authoritative relay state; run `validate_plan` before preview" (line 73). The recording/
session-report/watchdog work doesn't disappear — it resurfaces, undated, inside the
"Delivery-gated parallel lane" deliverable list: "operator-presence watchdog and session
reports (C)" (line 38). The only day-table trace of it is Sept 6's "support only
scheduled hardware watchdog work" (line 74), which bounds it but doesn't remove it.

So if the drone delivery gate opens during Sept 5–9 — plausible, since the PRD's own
framing is "drones arrive within a week" of Sept 1 (`docs/prd.md:248`) — C is
simultaneously finishing the compiler/validation/logging/cached-CI/50-item-eval slice
*and* standing up the operator-presence watchdog and session-report tooling for live
flights. The stress test acknowledges the general shape of this risk ("Hardware support
consumes A and C time... Unscheduled acceptance work would delay A's language/video
work or C's compiler/media work," line 91) but doesn't sequence or bound C's specific
watchdog/session-report obligation the way it bounds B's hardware-bring-up-vs-resolver
choice. Recommend explicitly stating in §8.4 whether the watchdog is scheduled before or
after the compiler slice, or reassigning it, rather than leaving it as an unscheduled
parallel obligation on the same owner in the same week.

No other double-booking found. A's Sept 5–6 work (typed input, preview/confirm, band
adapter) has no earlier claim on that time since glasses is cut — this is the freed
capacity working as intended.

## 3. Glasses-cut-is-back-end-neutral claim, checked directly against the PRD

Confirmed true for everything except the Band SDK question (item 5, below). I read
§4.1's system diagram, §4.2's component table, §5.9, §5.6 (adapters), and §5.5 (arbiter)
directly rather than trusting the draft's summary:

- §4.1's diagram shows the glasses web app as one more arrow into the same WebSocket
  relay, identical in shape to the webcam and language arrows — no glasses-specific
  relay logic exists to remove.
- §5.5 (arbiter) and §5.6 (adapters) never reference glasses, pinch, D-pad, or head
  direction — the safety and hardware layers are genuinely input-agnostic, as §0 claims.
- The only glasses-specific back-end-adjacent item is hosting: §7.5 says "the console and
  the glasses app are static files served from the laptop for development and from
  GitHub Pages or Vercel for the glasses (which require a public HTTPS URL)." Cutting
  glasses removes a C deployment target, which is a simplification, not a broken
  dependency.
- One thing the draft's cleanup list (line 101) doesn't name but should: §7.1's failure-
  mode table has a degradation ladder — "full → no video → no language → no glasses →
  webcam only → keyboard e-stop only" — that still needs the "no glasses" rung removed.
  Minor, but worth adding to the enumerated cleanup list since the draft's list is meant
  to be the checklist for the later mechanical pass.

Verdict: the back-end-neutral claim holds for the relay/planner/arbiter/adapter surface
exactly as stated. It does not extend to the Band's *input* surface, which is the item 5
finding above, not a back-end concern but a client-side one the draft conflated with
"needs no new plumbing."

## 4. Does moving language ahead of Phase 3 break any dependency?

No, in either direction. Phase 5's original entry criterion was "intent contract stable;
relay exposes state to the language module" (`docs/prd.md:281`) — no perception or video
dependency. Phase 3's original entry criterion was "one camera source available"
(`docs/prd.md:269`) — no language dependency. Detections reach the relay purely as
events (§5.7) and never pass through the language module. The stress test's claim 7
("Language does not depend on video or perception," line 92) checks out — I found
nothing in either phase's original spec that assumes the other has already shipped.

## 5. Band-rides-the-webcam-boundary claim, checked against the scaffolding draft

This is the headline finding — see the top of this document. To restate the mechanics:
A2's "pure boundary" (`docs/scaffolding-review-round1-dev.md:91`) only covers turning an
already-recognized semantic action into an Intent v1 JSON payload. It says nothing about
where the semantic action itself comes from. For the webcam, that's MediaPipe hand-
tracking running in the browser — a completely different stack from the Meta Web Apps
SDK the PRD assigns to glasses+Band (`docs/prd.md:118`). The PRD gives no evidence the
Band emits raw gesture events to anything other than that SDK, and its one description of
Band integration work is literally scoped as glasses-app work: "glasses web app emitting
the intent set from **band gestures** and head direction (A)" (`docs/prd.md:276`).

If that SDK dependency is real (I can't rule it out from the docs alone — this needs a
five-minute check against Meta's actual Neural Band/Ray-Ban Display developer docs, which
neither draft did), then "the band event adapter on the webcam producer boundary" (Sept 6,
A, line 74) is not a half-day task alongside "wire confirmed plans to ordered relay
emission" — it's a second SDK integration inside a different host page, plus whatever
network/HTTPS/pairing requirements that SDK carries (the glasses' own HTTPS hosting
requirement, §7.5, exists for exactly this kind of reason). That could be genuinely new
work with its own estimate, possibly requiring the same kind of hosting C previously did
for glasses (line 9's "no new web application, no new phase" claim would then be false
for at least the "no new web application" half).

**Recommendation:** before this goes to Koby, confirm with Meta's Neural Band developer
documentation (or by testing pairing against a bare webcam-console page) whether Band
gesture events are obtainable independent of the Ray-Ban Display glasses/app. If they are
independent, the draft's claim stands and Sept 6's estimate is fine. If they are not, this
becomes a real open question about whether "the Band pairs with the webcam console" is
achievable as scoped at all inside the capstone window — which is a product question for
Koby, not a scheduling detail to fix quietly. This is exactly the kind of decision the
brief said was "not up for debate" for the reviewers to make unilaterally, but the
feasibility gap underneath it is real and should be surfaced before the schedule is
approved on the assumption it's free.

## 6. Overall verdict

The reorder (Phase 1 → language vertical slice → hardware-parallel → video → language
completion → hardening) is realistic and the vertical-slice cut for Sept 5–9 is the right
size for what C and A can deliver without B. Two schedule-hygiene fixes needed, neither
blocking: relabel the Sept 5/6 "otherwise, resolver work" cells in B's row so Phase 2
doesn't silently borrow Phase 5 scope, and give C's operator-presence-watchdog/session-
report obligation an explicit sequence relative to the compiler slice instead of leaving
it as an unscheduled parallel claim on the same person in the same week.

The one item that should stop this from going to Koby as "verified" is the Band/glasses
SDK coupling. It's stated as a settled fact twice in the draft (lines 9 and 90) when the
PRD's own text — the phase title, the deliverable line, and the component table — points
the other way, and the scaffolding draft explicitly left it open rather than resolved.
Confirm the actual SDK boundary before this resequencing is presented as fully feasible.
