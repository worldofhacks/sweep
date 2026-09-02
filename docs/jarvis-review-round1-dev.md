# Jarvis architecture proposal: development review, round 1

## Summary

The proposal fits the capstone only if it produces no Phase 1 or Phase 2 implementation work. The current PRD already preserves the useful extension points: inputs emit a shared intent contract, the planner alone lowers intents to commands, adapters isolate hardware, and the Phase 5 language path uses deterministic resolvers. Those boundaries are enough to support later generalization.

Change 1 is not free. Adding `capabilities()` changes the adapter interface that freezes at 9:00 a.m. on Sept 2 (§8.2 and Appendix C). It adds design, implementation, and contract-test work for B during the Sept 2 sim-adapter work and risks expanding B's Sept 4 hardware bring-up. A descriptor with camera, gimbal, GPS, and formation fields also reaches beyond the current adapter responsibility in §§4.2, 4.5, and 5.6. Since the scripted mission does not consume capability discovery, implementing it before Phase 6 conflicts with §8.6.

Change 2 can fit Phase 5 only as the implementation strategy for the already-scheduled language module and its acceptance demonstrations. A local command router can be a second compiler into the existing plan schema, followed by `validate_plan`, preview, confirmation, relay emission, planner lowering, and arbitration. It still requires router logic, ambiguity rules, gold-set cases, integration tests, and preview behavior. It adds work for C, A, and B in their Phase 5 assignments (§§5.10, 6 Phase 5, 8.4). If it is treated as optional breadth beyond the Phase 5 exit test, §8.6 defers it until after Phase 6. It creates no Phase 1 or Phase 2 cost if the team defers it completely.

Changes 3 and 4 are viable post-capstone directions, with a narrow Phase 5 subset for the already-planned selection and location resolvers. Refactoring the Phase 1 planner around a new primitive command layer would directly disrupt B's critical path. Building a semantic world model would pull telemetry, maps, detections, relay state, perception, and language resolution into a new shared subsystem. Both are off the scripted mission path in their generalized form and therefore remain post-Phase-6 work under §8.6.

## Review of the four changes

### 1. Add `capabilities()` to `SwarmAdapter`

**Verdict: costs Phase 1 and can spill into Phase 2. Do not add it now.**

The interface in Appendix C has six operational methods. Section 8.2 freezes that interface on Sept 2. Adding a capability descriptor requires decisions about field semantics, units, required versus optional values, static versus runtime values, and unsupported capabilities. A useful implementation also needs contract tests against the sim adapter and whichever hardware adapter B selects. An unused method would add interface surface without proving compatibility.

The proposed fields cross existing component boundaries. `position_control`, `velocity_control`, `max_speed`, and vehicle constraints belong near B's adapters, planner, arbiter, and mode parameters (§§4.2, 5.3 to 5.6, 8.1). `camera` and `gimbal` currently belong to the media and perception path owned by A and C (§§4.2, 5.7). `formation` is a planner behavior in §§4.2 and 5.3, not an adapter operation. A single descriptor would therefore need a boundary decision before it could become a stable contract.

Current-week impact:

- **B:** expands the Sept 2 sim-adapter and planner work, plus adapter contract tests. It can also expand the Sept 4 hardware adapter bring-up when the real drone model becomes known (§8.3).
- **C:** must version or publish the changed frozen contract and incorporate its tests into CI if the descriptor crosses relay or schema boundaries (§§8.1 to 8.3).
- **A:** no direct week-one deliverable unless camera capabilities are surfaced in the console. Doing that now would add an off-mission feature.

The forward-compatible choice is to keep Appendix C unchanged during the capstone. A versioned adapter-capability contract can be designed after Phase 6 using evidence from the sim and real hardware adapters.

### 2. Add local and LLM language paths

**Verdict: conditionally compatible in Phase 5, with material Phase 5 cost. Otherwise defer it until after Phase 6.**

Section 5.10 defines one language pipeline: compile a plan, validate it, preview it, confirm it, then emit intents through the relay. A local router can replace the LLM call for a constrained subset while preserving that pipeline. This keeps §5.1 intact because the router emits intents or a plan of intents, and the planner remains the only component that produces per-drone commands.

The latency argument does not justify a fast execution bypass. The PRD allows up to two seconds for language plan preview (§§2 and 3.2), and §§3.1, 3.3, and 5.10 require language plans to be previewed and approved. E-stop has its own sub-100 ms path, is owned by the safety arbiter, and must run without a model (§§3.2, 3.3, 5.5). Spoken `stop` also needs an explicit meaning: `hold` and `estop` have different safety semantics.

Phase 5 impact:

- **C:** owns the local router, its shared plan representation, LLM compiler plumbing, cached evals, fallback behavior, and route-equivalence tests (§§4.2, 6 Phase 5, 8.4).
- **A:** owns preview and confirmation behavior for locally routed plans on laptop and glasses (§§4.2, 5.8 to 5.10, 8.4).
- **B:** owns deterministic selection and location resolution and must define operational semantics for phrases such as `stop`, `nearest two`, and altitude steps (§§4.5, 6 Phase 5, 8.4).

This change is forward-compatible with Phase 1 only as a deferred design constraint. No router code, schema branch, or UI work should enter the Phase 1 or Phase 2 schedule. During Phase 5, it must replace part of the scheduled compiler work and be exercised by the Phase 5 language acceptance demonstrations. An additive convenience path that the acceptance run does not use remains barred by §8.6 until after Phase 6.

### 3. Build composed behaviors over universal primitives

**Verdict: a post-Phase-6 planner redesign, not a Phase 1 refactor.**

Section 5.3 intentionally makes formation and sweep deterministic planner behaviors. They are central to the scripted mission in Appendix E and B must implement and test them on Sept 2 and run them on six drones by Sept 6 (§8.3). Replacing those direct planner cases with a new behavior-composition layer during Phase 1 would add an intermediate representation, lowering rules, failure semantics, arbiter coverage, and regression tests to B's critical path.

Several proposed “universal” primitives do not exist in the current adapter contract or intent contract. Appendix C has `takeoff`, `goto`, `hover`, `land`, `estop`, and `telemetry`. `move_relative`, `set_altitude`, `set_heading`, `return_home`, `follow_path`, `camera_capture`, and `camera_aim` would require decisions across the planner, arbiter, adapters, perception/media, and tests. Exposing those operations to an input or plan compiler would violate §5.1 because only the planner may produce commands.

Owner impact if attempted now:

- **B:** planner, arbiter, sim adapter, hardware adapter, and sim scenarios (§§5.3 to 5.6, 8.1, 8.3).
- **A:** camera behavior and any gesture, console, or glasses support (§§5.7 to 5.9, 8.1).
- **C:** intent or command schemas, logging, replay, and CI coverage (§§5.2, 8.1 to 8.3).

After Phase 6, B can introduce a planner-internal primitive representation while keeping inputs on the intent contract and adapters on versioned operational commands. New `inspect` or `search` intents are acceptable only as a complete vertical change: contract, tests, and all three input paths, as required by §8.6.

### 4. Generalize resolvers into a world-grounding layer

**Verdict: keep the existing deterministic resolvers in Phase 5; defer a semantic world model until after Phase 6.**

The proposal's narrow core already exists in the PRD. Section 4.5 specifies `get_state`, `resolve_selection`, and `resolve_location`; §5.10 uses map and operator heading for spatial phrases; Phase 5 assigns the resolvers to B. Implementing those functions against the relay's authoritative state is normal Phase 5 work.

A semantic world model is a larger subsystem. “Closest drone to that doorway” needs a typed representation of mapped or detected doorways, coordinate transforms, freshness and confidence rules, ambiguity handling, and operator confirmation. That work crosses B's resolver and allocation ownership, C's relay and telemetry state, and A's perception output (§§4.2, 4.5, 5.2, 5.3, 5.7, 8.1). It is not part of the Appendix E scripted mission.

The layer must preserve two current rules. Detections are events and never commands (§5.7), and a detection must be confirmed before it changes swarm behavior (§§3.1, 3.3, 4.8). Any learned perception or language model may supply candidates, but deterministic validation, operator confirmation, planner lowering, and arbitration remain mandatory. A model cannot enter the safety path (§8.6).

Phase impact:

- **Phase 5, B:** implement the specified selection and location resolvers over existing state and map inputs.
- **Phase 5, C:** expose the state required by the language module and cover resolver/compiler integration in evals.
- **Phase 5, A:** present clarification, preview, and confirmation choices.
- **Post-Phase-6, A/B/C:** design and implement a shared semantic world representation only after telemetry, maps, and detection events have stable observed shapes.

## Explicit PRD conflicts and required guardrails

1. **Frozen contracts:** `capabilities()` changes Appendix C after the adapter interface is designated for the Sept 2 freeze (§8.2). It needs a versioned contract decision and tests, not an informal additive method.
2. **Scripted-mission priority:** capability discovery, generalized behavior composition, semantic doorway grounding, new adapters, and new inspect/search behavior are outside Appendix E. Implementing any of them before Phase 6 conflicts with §8.6.
3. **New intents:** `inspect` and `search` cannot enter the current intent set unless the contract changes, tests cover them, and webcam, glasses, and language inputs all implement them (§8.6). The proposal's deferral statement is therefore mandatory.
4. **Planner ownership:** local language routing and composed behaviors may emit only existing intents. They cannot emit adapter primitives or per-drone commands because §5.1 reserves intent-to-command lowering to the planner.
5. **Language confirmation:** a fast local route still passes through plan validation, preview, and operator confirmation (§§3.1, 3.3, 5.10). Direct emission would violate the stated language safety flow.
6. **Safety path:** neither the LLM route nor any learned world-grounding component may participate in e-stop, battery return, command validation, or other arbiter decisions (§§3.2, 3.3, 5.5, 8.6).
7. **Detection action:** semantic grounding cannot turn a detection directly into movement. Detections remain events, and the operator confirms them before swarm behavior changes (§§3.1, 3.3, 5.7).

## First-pass epic and ticket map

### Phase 1: no additional Jarvis tickets

The current Phase 1 contracts and scripted mission already establish the needed seams. Adding a Jarvis-specific ticket would compete with the Sept 2 to 4 exit test. The team should freeze and implement the PRD as written.

### Phase 5: language work

| Epic / ticket | Owner | Scope and acceptance |
|---|---|---|
| Local router for a frozen command subset | C | Compile an explicitly enumerated set of utterances into the same plan schema as the LLM compiler. Unknown or ambiguous utterances fall through to clarification or the LLM path. Keep this ticket in Phase 5 only if the accepted language demonstrations exercise it; otherwise move it after Phase 6 under §8.6. |
| Define local-route command semantics | B | Specify mappings for `hold`, `come_home`, selection, and altitude steps. Resolve or exclude ambiguous `stop`; do not treat it as a voice e-stop shortcut. |
| Shared validation, preview, and confirmation flow | A + C | Both compiler paths call `validate_plan`, render the same preview, require the same confirmation, and emit intents through the relay. |
| Route-equivalence and ambiguity evals | C | Add gold cases showing equivalent local and LLM plans, safe fallback for unsupported language, zero direct command emission, and zero unsafe intents. |
| Deterministic selection resolver | B | Resolve supported expressions from authoritative swarm state and return clarification options for ambiguous inputs, as specified in §4.5. |
| Deterministic location resolver | B | Resolve the Phase 5 map and operator-heading expressions defined in §§4.5 and 5.10. Return clarification rather than inventing semantic entities. |
| Resolver clarification UI | A | Present resolver choices in the existing preview and confirmation surfaces on laptop and glasses. |

### Post-Phase-6 / outside the capstone

| Epic / ticket | Owner | Scope and acceptance |
|---|---|---|
| Adapter capability contract v2 | B + C | Define boundaries, units, optionality, versioning, and contract tests using evidence from the sim and real adapter. Keep media capabilities separate if they do not belong to flight control. |
| Additional vehicle adapters | B | Implement MAVSDK, DJI, or Autel adapters only against a proven versioned contract, with hardware-specific capability tests and safety behavior. |
| Planner-internal primitive representation | B | Design a typed internal command or behavior representation. Inputs continue to emit intents; only the planner lowers them; the arbiter validates the resulting commands. |
| Migrate existing composed behaviors | B | Move formation and sweep to the internal representation with scenario parity against the capstone implementation before adding behaviors. |
| Semantic world-state contract | A + B + C | Define entities, poses, coordinate frames, provenance, confidence, and freshness across telemetry, maps, and detections. Keep raw detections as events. |
| Grounded entity resolvers | B | Resolve entities such as mapped doorways through deterministic candidate selection, clarification, and operator confirmation. |
| Camera and gimbal command boundary | A + B | Decide whether capture and aim belong in flight adapters, media/perception services, or a separate device interface before adding commands. |
| End-to-end `inspect` or `search` intent | A + B + C | Change the intent contract, update webcam, glasses, and language inputs, add planner and arbiter behavior, and add contract and scenario tests as one vertical feature. |

### Rejected / out of scope

| Proposal variant | Reason |
|---|---|
| Add `capabilities()` during Phase 1 or hardware bring-up | Changes a frozen contract, adds B/C work on the critical path, and serves no Appendix E mission step (§§8.2, 8.3, 8.6). |
| Add capability-driven console, camera, gimbal, GPS, or formation features before Phase 6 | These features are outside the scripted mission and cross component ownership (§8.6). |
| Let the local language router emit per-drone or adapter commands | Violates the planner-only lowering rule (§5.1). |
| Let locally routed language skip validation, preview, or confirmation | Violates the language plan flow and human-in-the-loop rules (§§3.1, 3.3, 5.10). |
| Route spoken `stop` directly to e-stop through the language module | E-stop belongs to the arbiter's always-live, sub-100 ms, model-free safety path (§§3.2, 3.3, 5.5). |
| Refactor the Phase 1 planner around a universal primitive layer | Adds risk to B's Sept 2 to 4 planner, arbiter, sim, and scenario deliverables and is unnecessary for the scripted mission (§§5.3, 8.3, 8.6). |
| Add `inspect` or `search` as partial or capstone-era intents | Violates §8.6 unless the contract, tests, and all three inputs change; the behavior is outside the scripted mission. |
| Let a model or raw detection choose and dispatch movement | Violates the model-free safety rule and the requirement that detections be confirmed before changing swarm behavior (§§3.1, 3.3, 5.7, 8.6). |
| Build a generalized semantic world-model subsystem during Phases 1 to 5 | It is outside Appendix E and consumes A/B/C capacity reserved for the capstone phase exits (§§6, 8.3, 8.4, 8.6). |

## Conclusion

The architecture can remain compatible with the Jarvis direction by preserving the current boundaries and postponing new abstractions until observed adapter, planner, and perception data justify them. Phase 1 and Phase 2 should follow the PRD unchanged. Phase 5 can add the already-specified deterministic resolvers. It can also use a local language compiler when that path is part of the scheduled acceptance demonstrations and preserves validation, preview, confirmation, planner ownership, and arbitration. Capability negotiation, universal primitives, semantic world state, new adapters, and new intents belong after Phase 6.
