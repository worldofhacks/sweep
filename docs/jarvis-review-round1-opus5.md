# Jarvis architecture proposal: independent second review

## Bottom line

I independently re-derive the same top-level verdict as round 1: none of the four
changes are free right now, Phase 1 takes zero new tickets, and the proposal's claim
that items 1 and 2 are "low-cost, forward-compatible additions" is false for item 1
and only conditionally true for item 2. I agree with round 1's mechanics on changes 3
and 4 without material disagreement. On change 1 and change 2 I converge on the same
verdict but for a sharper reason in each case, and I found one concrete factual gap
round 1 missed: the proposal's own wording for change 2 references an intent, `stop`,
that does not exist in Appendix A.

## Per-change verdicts (independent derivation)

### 1. `capabilities()` on `SwarmAdapter`

**Verdict: not free now. Agree with round 1's rejection, disagree with its stated
mechanism.**

Round 1 frames the cost as "changes the adapter interface that freezes at 9am Sept 2."
That's a real constraint, but the review was filed at 23:05 on Sept 1 — before that
freeze. Chronological proximity to a deadline isn't itself a cost; the cost is what
that deadline forces into the next few hours. §8.2 lists Appendix C as a contract
"frozen on day one," meaning whatever ships in the interface at 9am Sept 2 is what
Phase 1–2 adapters (`sim`, then `crazyswarm2`/`mavlink`) get built and tested against
with no do-over. Cramming a capability descriptor in before that freeze doesn't avoid
the cost, it just moves it earlier: someone (B, since adapters are B's) has to decide
field semantics, units, optional-vs-required, and static-vs-runtime *tonight*, without
the design review or field evidence from a working adapter that would normally inform
that decision. That is the actual cost — rushed, unvalidated design baked into a
contract meant to hold for the rest of the build — not a type-checker failure. Worth
noting for precision: the PRD's CI plan (§4.7, §7.3) is pytest plus gold sets plus a
sim scenario runner; there's no mypy/pyright gate mentioned, so an unimplemented
`Protocol` method wouldn't even fail CI mechanically. The reason to reject this now is
governance and boundary-ownership, not enforcement.

I'd also sharpen round 1's owner attribution: `capabilities()` as literally scoped
(fields on `SwarmAdapter`) doesn't have to cross the relay or intent schema at all — it
can stay entirely inside B's adapter/planner boundary. Round 1 already hedges C's
involvement with "if the descriptor crosses relay or schema boundaries," and I'd go
further: as scoped, it shouldn't need to. C's exposure here is close to zero unless the
team also decides the console needs to *display* capabilities, which is new
scope nobody asked for. That doesn't change the verdict — B still absorbs unplanned
design and contract-test work on the Sept 2 critical path — but it changes who's
actually on the hook.

### 2. Local + LLM language paths

**Verdict: conditionally viable in Phase 5 only, agree with round 1's frame. One gap
round 1 missed.**

Round 1 correctly rejects the latency argument (2s preview budget is already generous)
and correctly flags that spoken `hold` and `estop` carry different safety semantics.
What round 1 doesn't say explicitly: the proposal's own change-2 text lists the local
router's target commands as "`hold`, `stop`, `come_home`, selection, altitude step" —
but Appendix A's intent enum is `arm | disarm | estop | select | takeoff | land |
land_all | hold | translate | altitude | formation_next | formation_set | spacing |
come_home | sweep`. There is no `stop` intent. The proposal is one lexical layer away
from being unimplementable as written — a router author would have to invent a mapping
from the spoken/typed word "stop" to either `hold` or `estop`, which is precisely the
ambiguity round 1 warns against, except it isn't a hypothetical the router might
introduce later; it's already latent in the proposal's own vocabulary. Any Phase 5
ticket for this needs to name the mapping explicitly (I'd default "stop" → `hold`,
never → `estop`, and require a distinct wake-word or UI affordance for true e-stop) and
that decision belongs to B per §5.5 ownership of the arbiter/e-stop semantics, not to
whoever writes the router.

Otherwise I agree with round 1's structure: the router is a second compiler into the
same plan schema, still gated by `validate_plan` → preview → confirm → planner → arbiter
per §5.10, contributes zero Phase 1/2 cost if deferred, and only belongs in Phase 5 if
it's the implementation of the already-scheduled language module rather than bolted-on
extra scope (§8.6).

### 3. Composed behaviors over a universal primitive layer

**Verdict: post-Phase-6 planner redesign. No disagreement with round 1.**

Formation and sweep are load-bearing for the Appendix E scripted mission and are on
B's Sept 2 and Sept 6 deliverables (§8.3). Re-platforming them onto a new primitive
representation before Phase 6 adds an intermediate representation, new failure
semantics, and new arbiter/test surface to B's critical path for no scripted-mission
benefit — exactly the §8.6 "no feature that isn't on the scripted mission path" rule.
Most of the proposed primitives (`move_relative`, `set_heading`, `follow_path`,
`camera_capture`, `camera_aim`) don't exist in Appendix C today and would need their
own design pass regardless of when they land.

### 4. World-grounding layer

**Verdict: narrow version already in scope for Phase 5 (§4.5's `resolve_selection` /
`resolve_location`), general version deferred post-Phase-6. No disagreement with
round 1.**

The generalized version ("closest drone to that doorway") needs typed map/detection
entities with freshness and confidence, which the perception pipeline doesn't produce
yet — §5.7 detects "people and common objects," not structural features like doorways,
and only from Phase 3 onward. Building the general resolver now would require
perception work that hasn't happened and isn't scoped for Phase 3 as written. Both
versions must keep detections as events requiring operator confirmation (§5.7, §8.6);
neither may let a model pick a movement target.

## PRD conflicts — confirmed against my own read

I re-checked each cited section directly rather than trusting round 1's citations:

- §8.2 lists Appendix C ("adapter interface") as one of four contracts frozen 9am
  Sept 2 — confirmed, change 1 hits this squarely.
- §5.1: "the planner is the only thing that turns intents into per-drone commands" —
  confirmed verbatim; rules out any input or compiler emitting adapter-level commands
  (change 3's failure mode if primitives leak past the planner boundary).
- §8.6: "No new intents without a contract change, a test, and all three inputs
  updated. No model in the safety path. No feature that isn't on the scripted mission
  path until Phase 6." — confirmed verbatim; this is the operative rule for all four
  changes' timing, not just a general caution.
- §5.5: e-stop and battery return are explicitly the two behaviors that "ignore all
  inputs" and run with "no I/O," i.e., model-free — confirms the `stop` ambiguity in
  change 2 is a real safety-semantics question, not a naming nitpick.

I found no misquoted or fabricated citation in round 1.

## My own epic/ticket list

I derived this independently before re-reading round 1's table in detail; it converges
closely, which I read as corroboration rather than copying.

### Phase 1: zero Jarvis tickets

The PRD ships as written. Nothing above is on the Appendix E scripted mission path.

### Phase 5 (language work)

| Ticket | Owner | Notes |
|---|---|---|
| Local command router for a fixed utterance subset | C | Same plan schema as the LLM compiler; falls through to LLM/clarification on anything unrecognized. |
| `stop` → `hold` mapping decision, e-stop kept out of the language path entirely | B | Must be an explicit written mapping, not left to whoever implements the router. Direct consequence of the Appendix A gap above. |
| Router/LLM route-equivalence gold cases | C | Prove both paths produce identical validated plans for the overlapping subset. |
| Shared `validate_plan` → preview → confirm flow for both compiler paths | A + C | No route bypasses preview/confirm. |
| `resolve_selection` implementation | B | Per §4.5, against relay state. |
| `resolve_location` implementation | B | Per §4.5/§5.10, map + operator heading only. |
| Resolver clarification UI | A | Laptop + glasses preview surfaces. |

### Post-Phase-6 / out of scope

| Ticket | Owner | Reason it's deferred |
|---|---|---|
| Adapter capability contract v2 | B | Needs real sim + hardware adapter evidence before the field list is trustworthy; §8.2 freeze blocks doing it pre-Phase-6. |
| Additional adapters (MAVSDK/DJI/Autel) | B | Not in capstone scope (PRD §2 non-goals); depends on capability contract v2. |
| Planner-internal primitive representation | B | Formation/sweep stay as direct planner cases through the capstone; §8.6. |
| General semantic world model (doorways, etc.) | A + B + C | Depends on perception entity types that don't exist until Phase 3 does more than person/object detection. |
| `inspect`/`search` intents | A + B + C | Requires the full §8.6 vertical: contract change, tests, all three inputs. |

### Rejected outright

| Item | Reason |
|---|---|
| `capabilities()` before the Sept 2 freeze | Forces unvalidated design onto a contract meant to hold through Phase 2; §8.2. |
| Router emitting adapter/per-drone commands | Violates §5.1. |
| Router bypassing preview/confirm for speed | Violates §5.10 and the 2s budget is already generous; no latency case for a bypass. |
| Spoken "stop" routed to e-stop | §5.5 requires e-stop to be model/language-free; conflates `hold` and `estop`, two different safety states. |
| Universal primitive layer before Phase 6 | Adds unbounded risk to B's Sept 2–6 critical path; §8.6. |
| General world-grounding layer before Phase 6 | Perception doesn't yet produce the entity types it needs; §5.7/§8.6. |

## Where I agree and disagree with round 1, explicitly

**Agree, no material difference:** changes 3 and 4 verdicts and reasoning; the §5.1
and §8.6 conflict analysis; the Phase 5 ticket ownership split (matches §8.1/§8.4
almost exactly, which is a good sign both reviews are reading the same document
correctly rather than one anchoring on the other).

**Agree on verdict, disagree on mechanism:** change 1. Round 1 leans on "breaks the
frozen interface" as if the freeze had already happened; I'd lean on "forces
unvalidated design in the hours before a freeze that's supposed to lock in
field-tested decisions," and I'd narrow C's exposure closer to zero than round 1's
hedge implies, since the descriptor doesn't have to cross the relay/schema boundary as
scoped.

**Material addition round 1 missed:** the proposal's change-2 text names a `stop`
command that isn't in Appendix A's intent enum. This isn't cosmetic — it means the
router, as described, can't be implemented without first resolving an ambiguity the
PRD's own safety split (§5.5: e-stop is model-free and separate from `hold`) already
answers, and any Phase 5 ticket for the router needs to encode that mapping explicitly
rather than leaving it to the implementer.

I found nothing in round 1 that is factually wrong or that misreads a PRD section.
