# Input and capability-eval scaffolding: issue drafts, round 1

## Recommendation

Koby's narrower goal needs five tickets. Three make the input contract executable for webcam, glasses, language, and a wrist or neural band. Two extend the existing adapter capability and language-eval issues into a capability-aware model benchmark. None requires a universal command layer, semantic world model, or new intent.

The repository currently contains prose descriptions and empty package scaffolds. Appendix A is the only intent definition, `tests/test_layout.py` is the only test, and the committed tree has no `console/` directory or Phase 0 webcam artifact. There is no machine-readable intent schema, source-registration record, reusable source conformance suite, producer runner, adapter protocol implementation, or eval runner. Section 5.1's input-extension rule therefore has no enforcement today.

The input work belongs to the existing capstone path. The canonical schema and webcam runner are Phase 1 deliverables. Glasses and band conformance are Phase 4 deliverables. The registration mechanism is a checked data file plus native tests in each source component. It does not need a runtime plugin manager, shared input class hierarchy, or relay discovery service. A new source adds its identifier, implementation, and conformance runner while the relay, planner, and arbiter remain unchanged.

The capability benchmark has two boundaries. Issue #1 owns the five-field `CapabilityDescriptor` and its sim implementation. The first new ticket uses that descriptor to decide whether planned adapter calls are supported. The second extends issue #2's Phase 5 compiler eval so real model output is schema-checked, planned, arbitrated, and scored against the selected capability profile. It measures the current drone path first. Synthetic limited profiles can prove that the harness reports unsupported combinations. Rover, boat, and RC-car runtime support still requires future adapters and vehicle-specific contracts.

Issue #3 should be narrowed in place and renamed. Its adapter portability items still match Koby's goal, while its primitive layer, semantic world state, grounded entities, camera/gimbal boundary, and `inspect`/`search` intents describe the broad direction he has rejected. Preserving the issue keeps its discussion history; rewriting its scope removes the misleading roadmap.

## Current gaps

| Required property | Current repository state | Consequence |
|---|---|---|
| One frozen intent contract | Appendix A prose only | Producers and relay validation can drift. |
| Extensible source identity | Appendix A lists `webcam`, `glasses`, `language`, and `keyboard` as a closed example | Registering `band` would otherwise require editing relay validation. |
| Shared source contract tests | No intent tests or fixtures | Section 5.1 and the Phase 4 glasses exit criterion cannot be enforced. |
| Test runs the producer | No source runner and no committed console | A fixture-only test could pass without exercising webcam, glasses, band, or language code. |
| Exact required source coverage | §5.1 says “ten intents plus `estop`”; Appendix A lists fifteen names | The freeze needs one explicit acceptance matrix. |
| Capability descriptor | Defined by open issue #1, not implemented in the current tree | Capability-aware eval must depend on #1. |
| Capability/action oracle | No planner, adapter, or eval implementation yet | A valid JSON plan cannot yet be distinguished from an executable plan for a given profile. |
| Model comparison harness | PRD §4.7 and issue #2 describe metrics and gold data only | Models cannot yet be compared through the real validation and planning path. |

## A. Input scaffolding

### Draft issue A1: Freeze Intent v1 and the shared input-source conformance suite

**Phase:** Phase 1, before producers and relay validation diverge

**Owner:** C primary; A supplies the browser-producer shape; B reviews motion argument semantics

**Estimate:** C 3 to 5 hours, A 1 to 2 hours, B 30 to 60 minutes

#### Problem

Section 5.1 accepts a new input source when it emits the required intent set and passes the webcam contract tests. The repository has neither the executable contract nor those tests. Appendix A also uses a closed-looking source list, which would force a relay schema edit when `band` is added.

#### Scope classification

- **Current capstone path:** encode Appendix A as the canonical Intent v1 schema, use it at the relay boundary, and make relay admission reject source identifiers absent from the registry. This is part of C's Phase 1 schema and relay work (§§6, 8.1, 8.3).
- **Scaffolding-only:** add shared valid/invalid cases, a producer-runner contract, and a contributor guide. These add no commands or planner behavior.
- **New work:** the reusable cases, registry check, and guide are additional C work. A's browser runner input shape adds a short review task.

#### Implementation

1. Add one machine-readable Intent v1 schema covering every Appendix A envelope field, every frozen intent name, and each intent-specific `args` shape. Relay validation and producer tests consume the same source artifact or a generated artifact from it.
2. Define `source` as a constrained identifier string instead of a hardcoded enum. Keep a checked registry of accepted source identifiers and their owning component. The relay reads that data when admitting an intent and rejects unregistered identifiers. Add an entry only when that source's implementation and conformance runner land: `webcam` and `keyboard` in A2, `glasses` and `band` in A3, and `language` in Phase 5. Adding a registry entry must require no relay, planner, or arbiter code change.
3. Add language-neutral valid and invalid cases for the envelope, every frozen intent, argument shapes, confirmation, unknown names, unknown arguments, missing fields, and malformed source identifiers.
4. Define a small producer-runner contract: a source's native test invokes its real intent-construction boundary for named semantic actions and submits the resulting JSON to the shared assertions. Browser sources can use their JavaScript test runner; Python sources can use pytest. The shared fixture does not count as coverage unless the source implementation produced the payload under test.
5. Document the registration steps: choose an identifier, add the registry entry, implement the source locally, add a native runner over the shared cases, and make CI pass. No relay endpoint or planner registration step belongs in this guide.
6. Resolve the discrepancy between §5.1's “ten intents plus `estop`” and Appendix A's fifteen intent names. Freeze one explicit required-source matrix before accepting a producer. Record which names are mandatory for every source and which are alternate forms, such as `formation_next` and `formation_set`.

#### Acceptance criteria

- The canonical schema rejects unknown intent names, unknown arguments, missing required arguments, invalid modes, and malformed source identifiers before planner invocation. Relay admission also rejects a well-formed source identifier that is absent from the registry.
- A registry test rejects entries without a conformance runner. A1 supplies the registry mechanism; each dependent source ticket adds its own entry and runner atomically.
- The shared suite covers every Intent v1 name and every non-empty `args` shape.
- At least one malformed payload is rejected by both the shared validator and the relay boundary.
- A producer runner must invoke source implementation code. A test that reads expected JSON and validates that same JSON does not satisfy this issue.
- CI exposes a single conformance result per registered source.
- The contributor guide shows that adding a source changes its own component, the registry data, and its tests only.

#### Non-goals

- Runtime plugin discovery, hot loading, dependency injection frameworks, or a new top-level `inputs/` service.
- Per-source capability descriptors.
- Gesture, speech, or handwriting recognition quality tests.
- Planner behavior, safety policy, vehicle capabilities, new intents, or changes to Appendix E.

### Draft issue A2: Restore the Phase 0 webcam source as the first conformance runner

**Phase:** Phase 1

**Depends on:** A1

**Owner:** A primary; C supplies the frozen WebSocket and authentication boundary

**Estimate:** A 0.5 to 1 day and C 1 to 2 hours if the Phase 0 artifact can be recovered

#### Problem

Phase 1 requires A to wire the shipped webcam console to the relay and remove its internal simulator (§§6 and 8.3). The current committed tree has no `console/` directory. The scaffold design also records that `swarm-gesture-console.html` was unavailable on the machine. A1 needs a real producer to prove the conformance suite tests implementation output.

#### Scope classification

- **Current capstone path:** restore the shipped console, connect it to the relay, and run Appendix E through the existing gestures.
- **Scaffolding-only:** isolate intent construction from recognition and WebSocket I/O so native tests can call it.
- **New work:** the isolation and conformance runner are additional A work. They must stay inside the existing wiring task.

#### Acceptance criteria

- Recover the shipped Phase 0 artifact from its actual source and place it under `console/`. Do not recreate recognition behavior from PRD prose.
- Put intent construction behind one small, pure boundary that accepts the recognized semantic action and returns an Intent v1 payload with `source=webcam`.
- Register `webcam` and invoke its real boundary from the native conformance test for every mandatory source action.
- Register `keyboard` and test the console's actual keyboard e-stop emission through the same envelope checks.
- Appendix E actions produce schema-valid payloads, including confirmation-bearing actions.
- Production events use the frozen relay topic and token contract. The internal simulator no longer consumes production gesture events.
- A disconnect or send failure is visible and emits no substitute motion intent. Keyboard e-stop remains available (§7.1).
- Console build, lint, and conformance tests run in CI.

#### Stop condition

If the Phase 0 artifact cannot be located, stop this ticket and open a recovery decision. Rebuilding its classifier and mappings is larger than Phase 1 relay wiring and needs a separate estimate.

#### Non-goals

UI redesign, recognition threshold changes, new gestures, a shared browser framework, glasses work, language routing, planner code, or safety code.

### Draft issue A3: Gate and register a Neural Band producer against Intent v1

**Phase:** Phase 2 access gate, then Phase 5 integration if access is available

**Depends on:** A1, A2, and evidence of a direct or partner Band event API independent of a glasses-hosted Web App

**Owner:** A primary; C wires the shared conformance checks into CI

**Estimate:** A 0.5 to 1 day and C 1 to 2 hours after the access gate passes

#### Problem

The Neural Band remains a capstone input, but the glasses application has been cut. Meta's public Web Apps path exposes Band navigation events inside an application rendered on Meta Ray-Ban Display glasses and deployed to the glasses over public HTTPS ([Meta developer announcement](https://developers.meta.com/blog/build-for-display-glasses/), [official starter kit](https://github.com/facebook/meta-wearables-webapp#run-on-your-meta-ray-ban-display-glasses)). No reviewed public Meta source exposes Band events directly to an arbitrary laptop browser page. The webcam console's A2 producer boundary is ready for Band semantic actions, but it does not provide device access.

#### Scope classification

- **Current capstone path:** once direct access is evidenced, Band actions drive the existing scripted mission through the webcam console, relay, planner, and arbiter.
- **Scaffolding-only:** the `band` registry entry and producer runner reuse A1 and A2.
- **New work:** verify access, then implement the native Band event-to-intent adapter and tests. The registry entry lands atomically with the real producer.

#### Acceptance criteria

- Record the exact SDK/API that supplies Band events independently of a glasses-hosted Web App. If none is available, leave the issue blocked and do not register a simulated producer as hardware support.
- After that gate passes, register `band` through A1's registry and invoke the real event-to-intent mapping from its native runner.
- D-pad, pinch, drag, or other supported Band events map only to frozen Intent v1 names.
- Held e-stop remains a direct deterministic mapping. No model or language router participates.
- Adding the source requires no relay, planner, or arbiter code change.
- The Band scripted mission and source conformance results pass in CI or the hardware-capable acceptance environment. Simulated keyboard events can test the seam but do not satisfy hardware acceptance.

#### Non-goals

The glasses-hosted Web App, public hosting for it, new gestures, per-device intent sets, source capability negotiation, dynamic discovery, or any new intent. Using Meta's documented glasses-hosted bridge requires an explicit product-scope exception and is not assumed by this ticket.

### Planned Phase 5 input follow-on

Issue #2 should add a `language` producer runner over the same schema after plan validation and confirmation. It tests emitted intents from the real compiler path. Speech transcription quality remains in the language gold set rather than the source contract suite.

## B. Capability/action-conversion evaluation

### Draft issue B1: Add a capability-profile execution oracle for the frozen mission

**Phase:** Phase 1 after exit-critical planner/sim work, or Phase 2 if it would delay the Sept 2 to 4 exit

**Depends on:** #1, “Phase 1: minimal `capabilities()` on `SwarmAdapter`”

**Owner:** B

**Estimate:** 3 to 4 hours for the capstone slice

#### How this differs from #1

Issue #1 owns the `CapabilityDescriptor` shape, the `SwarmAdapter.capabilities()` method, the sim descriptor, and the descriptor-shape unit test. This issue consumes that exact type. It does not add fields, define a second descriptor, or repeat #1's test.

#1 answers “what does this adapter report?” B1 answers “can this planned mission execute through the current adapter contract under that report?” The second question is the deterministic oracle the model benchmark needs.

#### Scope classification

- **Current capstone path:** run Appendix E through planner, arbiter, and the recording sim adapter; later parameterize the same acceptance with the selected hardware adapter.
- **Scaffolding-only:** store the reported profile with the run and return typed `supported`, `unsupported_capability`, or `contract_violation` results.
- **New work:** the recording adapter wrapper, profile checks, and report fields add 3 to 4 hours for B.

#### Implementation

1. Wrap the real sim adapter with a recorder that captures Appendix C calls and its #1 capability descriptor.
2. Run the frozen intents through the real planner and arbiter before the recorder. Inputs and models never call adapter methods directly.
3. Check current contract facts only:
   - indoor missions require `indoor_positioning=true`;
   - emitted `goto` calls require `position_control=true`;
   - `goto.speed` does not exceed either the active mode speed or `max_speed`;
   - every call is an Appendix C method.
4. Return a typed unsupported result when a profile cannot execute the current command contract. For example, `velocity_control=true` with `position_control=false` remains unsupported because Appendix C exposes no velocity operation.

#### Acceptance criteria

- Imports and uses #1's descriptor and `capabilities()` implementation.
- `sim_indoor` executes Appendix E through planner, arbiter, and recording adapter with zero capability or contract violations.
- The eval artifact records profile, intent, planned command, adapter call, result, and refusal reason.
- A speed-limit regression proves planner output respects both the mode limit and profile `max_speed`, in metres per second.
- A synthetic velocity-only profile produces `unsupported_capability` and no adapter dispatch.
- A synthetic profile without indoor positioning refuses the indoor mission before adapter dispatch.
- The same scenario can use the selected Phase 2 hardware adapter when it exists.
- No production capability branching is added solely for this test.

#### Non-goals

Universal primitives, velocity-command invention, new adapters, new intents, model calls, relay changes, UI, or claims that a rover or boat can execute the drone intent contract.

### Draft issue B2: Score plan-compiler models against adapter capability profiles

**Phase:** Phase 5

**Depends on:** #1, B1, and #2, “Phase 5 language path: local router + LLM dual compiler”

**Owner:** C primary; B owns capability rules and fixtures; A and all owners contribute the scheduled utterance set

**Estimate:** C 1 to 1.5 days and B 0.5 day beyond the base Phase 5 compiler eval

#### How this differs from and depends on #1

#1 supplies the adapter's capability profile. B1 turns that profile into a deterministic execution verdict. B2 measures a model or local router by running its actual output through the frozen intent schema, `validate_plan`, planner, arbiter, and B1. It does not modify `capabilities()` or infer new adapter operations.

Issue #2 owns the two compiler implementations and route-equivalence cases. B2 owns the comparison runner, profile-tagged dataset fields, and reports across compiler candidates.

#### Scope classification

- **Current capstone path:** extend PRD §4.7's 200-utterance language eval and Phase 5 exit test with capability-valid and unsafe-rate results for the sim and selected hardware profiles.
- **Scaffolding-only:** validate directly against Intent v1 and make the runner accept a capability-profile fixture, compiler callable, and deterministic execution oracle. A future vehicle adds fixtures and its reviewed validator without changing result storage or metric calculation.
- **New work:** capability-aware cases, compiler invocation, oracle integration, and reporting add C and B time in Phase 5.

#### Dataset case shape

Each case records:

- utterance and authoritative swarm state fixture;
- capability-profile fixture or captured adapter profile;
- expected intent plan, clarification, or unsupported result;
- compiler candidate and pinned configuration;
- expected safety and capability verdict.

The output plan remains a list of Appendix A intents. “Action conversion” in this harness means the full measured path from utterance to validated intent plan to deterministic planned adapter calls. The model never emits `goto`, velocity, or another adapter command.

#### Metrics

- schema-valid plan rate;
- exact-match plan accuracy;
- clarification or refusal accuracy;
- capability-valid execution rate;
- unsafe-intent rate, which remains zero;
- latency, token use, and estimated cost for live model runs;
- results grouped by compiler candidate and capability profile.

#### Acceptance criteria

- Runs the real candidate compiler or router for each uncached evaluation. A test that asserts cached expected output against itself does not count.
- CI can replay pinned cached responses, while a separate live command refreshes results for a new model or prompt.
- Every produced plan passes through the canonical Intent v1 validator, `validate_plan`, planner, arbiter, and B1 before receiving a capability-valid score.
- The initial report compares the Phase 5 LLM compiler and local router from #2 on the frozen drone intent set.
- `sim_indoor` and the selected hardware profile produce separate result groups.
- Limited synthetic profiles exercise B1's deterministic unsupported outcomes. They do not add runtime behavior or claim vehicle support.
- Failures identify the utterance, produced intent, planned call, required capability, reported profile, and reason.
- The Phase 5 thresholds remain at least 85% exact-match accuracy and zero unsafe intents (§§2 and 6).
- Adding a future vehicle benchmark requires a profile fixture, a reviewed vehicle validator, and dataset cases. The harness core and report schema remain unchanged.

#### Non-goals

- Asking a model to decide whether its own output is safe or executable.
- Model-generated capability profiles or capability rules.
- Model access to adapter commands.
- A universal vehicle intent set, universal primitive layer, or automatic translation from drone intents to rover, boat, or RC-car commands.
- Semantic world state, grounded entities, `inspect`, or `search`.
- New adapters or production capability routing.

## Scope and cost summary

| Ticket | Capstone behavior | Scaffolding only | New owner time |
|---|---|---|---|
| A1 | Canonical Intent v1, source registry, and relay admission | Shared cases, runner contract, guide | C 3 to 5 h; A 1 to 2 h; B 0.5 to 1 h |
| A2 | Webcam to relay and Appendix E | Pure emitter boundary and first runner | A 0.5 to 1 d; C 1 to 2 h |
| A3 | Phase 4 glasses/band mission input | Two registry entries and native runners | A 3 to 4 h; C 1 to 2 h |
| B1 | Frozen mission through sim/hardware adapter | Profile-tagged typed execution oracle | B 3 to 4 h |
| B2 | Phase 5 compiler eval on current drone path | Profile-aware runner and stable report shape | C 1 to 1.5 d; B 0.5 d |

If A1, A2, or B1 threatens the Phase 1 exit, the exit-critical relay, planner, arbiter, sim, and webcam wiring stay first. B1 can move to Phase 2. A1 cannot be reduced to fixture-only tests because that would leave the shared source contract unenforced.

## Rejected scope

- A common `InputSource` runtime framework, plugin loader, or hot-plug service.
- Per-source intent capabilities or separate intent vocabularies.
- Any new intent without the full §8.6 contract, test, and all-input update.
- Universal command primitives or a planner intermediate representation created for hypothetical vehicles.
- Semantic world state, grounded doorway or entity resolution, and model-directed movement.
- `inspect` or `search` under any name.
- Camera or gimbal capability design.
- A claim that the five fields in #1 are sufficient for every vehicle. Unsupported cases remain explicit until a real adapter provides evidence for a reviewed contract extension.

## Recommendation for GitHub issue #3

**Narrow issue #3 in place.** Rename it to `Epic: post-Phase-6 vehicle adapter portability` and remove “Jarvis” from the title and body.

Limit the renamed epic to adapter contract evolution, real vehicle adapters, and post-Phase-6 extensions of B1 and B2 for those adapters.

Keep only:

1. adapter capability contract v2, based on evidence from the sim and selected hardware adapter;
2. extend B1 and B2 after Phase 6 only for real rover, boat, or RC-car adapters;
3. additional adapters, each blocked by the reviewed capability contract and the capability/action eval.

Remove the current tickets for the planner primitive representation, formation/sweep migration, semantic world-state contract, grounded entity resolvers, camera/gimbal boundary, and `inspect`/`search`. Koby has identified that direction as scope creep, and none of it is necessary for input extension or model evaluation across vehicle profiles.

Closing #3 would discard useful discussion and its valid adapter-portability items. Leaving it unchanged would continue to advertise work Koby has rejected. Narrowing preserves the record and makes the remaining dependency chain explicit: #1, then B1/B2, then evidence-backed adapter contract changes and real vehicle adapters.
