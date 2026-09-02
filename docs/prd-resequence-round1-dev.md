# PRD resequencing draft: glasses cut, language second

## Feasibility verdict

The new order is feasible if the Sept 5 to 9 language phase is a complete vertical slice on sim rather than the full former Phase 5 scope. C cannot finish Phase 1 relay, logging, schemas, CI, and replay on Sept 4, then deliver the compiler, full 200-item corpus, cached eval, local fallback, observability, and hardware support by Sept 9. B also cannot lead delivery-gated hardware bring-up while building the full selection and location resolvers.

The early slice should deliver typed text through the real safety path: compiler, plan validation, preview, confirmation, ordered intent emission, planner, arbiter, and sim. A 50-item provisional gold set is enough to establish the ≥85% exact-match target and zero unsafe intents. Advanced resolver expressions, the full 200-item reviewed set, local fallback, speech accuracy work, and real-drone language acceptance remain in Phase 5. This preserves every existing language deliverable by Sept 19 while making language the second feature built.

The Neural Band cannot yet be scheduled as a direct webcam-console input. Meta's public developer path exposes Band input to Web Apps rendered on Meta Ray-Ban Display glasses, and deployment requires a public HTTPS URL added to the glasses through the Meta AI app ([Meta developer announcement](https://developers.meta.com/blog/build-for-display-glasses/), [official Web Apps starter kit](https://github.com/facebook/meta-wearables-webapp#run-on-your-meta-ray-ban-display-glasses)). The starter kit describes the EMG wristband as translating gestures to arrow keys inside that glasses-hosted app. No reviewed public Meta source exposes those events to an arbitrary laptop browser page.

The console's producer boundary remains the correct downstream seam once a direct Band event source exists, but it does not solve device access. Under the fixed glasses cut, real Band integration is blocked until Meta supplies a direct or partner API independent of the glasses-hosted Web Apps bridge. With such access, A needs roughly half to one day for mapping and native tests, and C needs one to two hours for registration and CI. Using the documented glasses-hosted bridge instead would retain glasses hardware, public HTTPS deployment, and a minimal glasses client. That would cost roughly one to two A days plus half a C day and requires an explicit scope exception; this draft does not assume it.

Cutting glasses leaves the back end unchanged. Section 5.9 describes an input client and display client; no relay, planner, arbiter, adapter, or safety rule calls into it. Removing that client deletes lens video, minimap, hosting, head-direction, pinch, and D-pad work. The replacement sections below contain no glasses dependency. The full PRD will still need a later mechanical cleanup of stale glasses references outside §§6, 8.3, and 8.4, but those edits are outside this draft.

## Replacement §6: Phased plan

Each phase has an entry criterion, deliverables, an exit test, and an owner per deliverable. Hardware work runs as a parallel lane because its start depends on physical delivery. Phase numbers express product priority rather than hardware arrival order.

### Phase 0: Webcam gesture console plus simulator (done, Sept 1)

- Deliverable: `swarm-gesture-console.html`, ten intents, dwell and confirmations, six-drone map sim, session recording, WebSocket intent emission.
- Exit: scripted run passes on video; recorded session saved.

### Phase 1: Intent bus, planner, arbiter, sim adapter, CI (Sept 2 to 4)

- Entry: intent schema frozen (morning of Sept 2).
- Deliverables: relay (C), planner and arbiter with unit tests (B), sim adapter (B), console wired to the relay instead of its internal sim (A), JSONL logging and replay tool (C), CI with unit tests and the first three sim scenarios (C), gesture gold-set v1 from Phase 0 recordings (A).
- Exit: the scripted mission runs end to end through relay, planner, arbiter, and sim, driven by webcam gestures, with zero unsafe intents in the log and the sim suite green in CI. Language work starts after this exit is green.

### Phase 2: Natural-language vertical slice and Neural Band access gate (Sept 5 to 9)

- Entry: Phase 1 exit green; Intent v1 stable; relay exposes authoritative state to the language module; planner, arbiter, and sim accept confirmed intents end to end.
- Deliverables: one pinned frontier compiler with schema-constrained plan output (C); typed laptop input with plan preview, clarification, confirm, and cancel in the webcam console (A); plan validation, JSONL logging, ordered relay emission, and cached-response CI (C); a 50-utterance provisional gold set covering the scripted mission, three multi-step orders, ambiguity, confirmation-sensitive intents, and unsafe requests (all); a documented Band access decision based on an actual direct or partner API independent of the glasses-hosted bridge (A).
- Boundaries: the early compiler uses existing mission intents, current selection, and explicit drone IDs. Natural-language selection and location expressions remain in Phase 5. Typed text is the required input; Web Speech and Whisper remain Phase 5 work. The model never emits adapter commands. Simulated keyboard events can test the console seam but do not count as Band integration or hardware acceptance.
- Exit: three typed multi-step orders execute through compiler, validation, preview, confirmation, relay, planner, arbiter, and sim; exact-match accuracy is at least 85% on the provisional set; unsafe-intent count is zero; ambiguous or invalid plans emit nothing. The Band access gate exits only when A can demonstrate an event source independent of a glasses-hosted Web App. If that evidence is unavailable, language exits on schedule and Band remains a named capstone blocker.

### Delivery-gated parallel lane: Real drones, indoor (five working days from arrival)

- Entry: drone model known, matching adapter path selected, positioning equipment available, guarded flight space ready, two-person flight crew booked, and Phase 1 exit green.
- Deliverables: `crazyswarm2` or `mavlink` adapter (B), hardware bring-up checklist and positioning calibration (B), one-drone acceptance, then three, then six (B with A on the console), battery return and link-loss behaviors verified on hardware (B), operator-presence watchdog and session reports (C).
- C sequencing: Sept 5 to 7 remains reserved for the compiler-to-sim critical path. If hardware arrives during that window, B may inventory equipment and run adapter ground checks using Phase 1 logging, but flight acceptance that requires the operator-presence watchdog waits. C implements the watchdog and full session reports on Sept 8 to 9 after the core language path is green. If the compiler path slips, provisional-corpus breadth defers before either safety path is partially implemented.
- Language integration: after Phase 2 exits, repeat its three multi-step orders through the hardware adapter. This repetition does not block the sim exit when hardware has not arrived.
- Exit: the scripted mission completes hands-free on six drones five times in a row; the safety log shows the correct refusal for a deliberately unsafe translate intent; the three language orders produce the same validated plans and safety outcomes observed on sim.

### Phase 3: Video and perception (Sept 10 to 15)

- Entry: Phase 2 sim exit green and one camera source available.
- Deliverables: MediaMTX ingest and WebRTC/MJPEG serving with recording (C), mosaic and focus-by-selection in the webcam console (A), detector on sampled frames with world-position estimates (A), attention promotion and thumb-up/thumb-down confirmation (A), detection events in the relay and logs (C), six sources on the dual-band network with latency measured when hardware is available (C).
- Exit: focus on a drone by holding up its number; a detection promotes its feed within one second; one-source latency meets the budget. Six-source acceptance remains tied to available camera hardware.

### Phase 4: Removed

The glasses application, lens video, minimap, alert line, head-direction controls, pinch/D-pad controls, hosting, and display recording are outside capstone scope. The Neural Band access check is part of Phase 2 and does not create a replacement hardware phase. A documented glasses-hosted bridge is not reintroduced under another name.

### Phase 5: Natural-language completion and integrated acceptance (Sept 15 to 19)

- Entry: Phase 2 language slice green; relay state remains stable; the hardware lane has reached the adapter stage if drones have arrived.
- Deliverables: `resolve_selection` and `resolve_location` with ambiguity handling (B); expansion to the 200-utterance gold set with responder review (all); full cached-response eval in CI (C); local-model fallback (C); Web Speech input and Whisper accuracy work when needed (A with C); final laptop preview and confirmation polish (A); Neural Band mapping, conformance, and hardware acceptance only if the direct-access gate has passed (A with C); three multi-step language orders repeated on real drones when the hardware lane is open (all).
- Exit: plan exact-match accuracy is at least 85% on the 200-item gold set, unsafe-intent count is zero, ambiguity produces clarification without emission, and three multi-step orders are demonstrated on real drones when hardware is available. Band completion additionally requires a real producer, its registered-source contract, and scripted input acceptance. Simulated keys are insufficient. Hardware and Band claims remain blocked until their respective evidence exists.

### Phase 6: Hardening, demo, release (Sept 19 to 24)

- Entry: Phase 2 sim language exit, Phase 3 video exit, Phase 5 full language exit, and delivery-gated hardware acceptance complete for every hardware claim included in the release.
- Deliverables: failure-mode drills (Section 7.1) on hardware, adversarial tests (Section 7.3), documentation and build guide, release, demo script and recorded reel.
- Exit: five consecutive scripted runs on hardware with no safety intervention; public repository tagged v0.1; demo reel cut. If hardware delivery blocks the five-run evidence, the software release may proceed but cannot claim the hardware exit.

## Replacement §8.3: Week one, by day

Phase 1 remains the only product work until its exit is green. Physical inventory may happen when hardware arrives, but it cannot displace the Phase 1 planner, arbiter, sim, relay, schema, console, logging, or CI work.

| Day | A | B | C |
|---|---|---|---|
| Sept 2 | Wire the webcam console to the relay; strip the internal sim; ship gesture gold-set v1 from Phase 0 recordings | Planner and arbiter with tests; sim adapter | Relay with WebSocket, token, authoritative state, JSONL logging; repo, CI skeleton, schemas |
| Sept 3 | Ledger, health strip, and replay view driven by relay state | Sim scenarios 1 to 5; battery and link-loss behaviors in sim | Replay tool; state fan-out; sim scenario runner in CI; gesture eval runner |
| Sept 4 | Run Appendix E through the webcam console and fix exit-blocking console defects; prepare the laptop language-input shell only after the exit is green | Complete planner, arbiter, sim, and unsafe-intent tests; begin hardware inventory only if Phase 1 is green and equipment has arrived | Complete end-to-end logging, replay, CI, and relay state exposure; freeze the language-facing state snapshot only after the Phase 1 exit |
| Sept 5 | Build typed laptop input, plan preview, clarification, confirm, and cancel in the existing console; document the Band direct-access gate | Lead one-drone hardware bring-up if the delivery gate is open; otherwise pull forward time-boxed Phase 5 resolver tests that do not gate Phase 2 | Define the plan schema; implement the pinned compiler against authoritative relay state; run `validate_plan` before preview |
| Sept 6 | Wire confirmed plans to ordered relay emission; prepare a Band mapping fixture only if a direct event API is evidenced | Continue hardware bring-up if open; otherwise pull forward Phase 5 resolver success, ambiguity, and refusal cases that do not gate Phase 2 | Integrate compiler logging, operator decision logging, ordered emission, and cached-response CI; defer watchdog work until the core compiler-to-sim path is green |

## Replacement §8.4: Weeks two and three, by phase

- **Phase 2, language slice and Band access gate (Sept 7 to 9):** C completes the typed-text compiler path, cached eval, error handling, and provisional 50-case report. A completes preview/confirm UX, documents whether an independent Band API is available, and contributes utterances. A implements the Band producer mapping and conformance runner only if that gate passes. All three finish the provisional set. B stays on hardware when the delivery gate is open; any resolver work pulled forward during idle time remains Phase 5 work and does not gate this exit. The language exit is earned on sim.
- **Delivery-gated hardware lane:** B owns adapter selection, positioning, calibration, and one/three/six-drone acceptance. A operates the console during booked flight blocks. C reserves Sept 5 to 7 for the compiler-to-sim path, then supplies the operator-presence watchdog and full session reports on Sept 8 to 9. Hardware inventory and ground checks may proceed earlier with Phase 1 logging, but watchdog-dependent flight acceptance cannot. Every flight still requires two people under §8.5; this support time is scheduled rather than treated as free capacity.
- **Phase 3, video and perception (Sept 10 to 15):** C brings up MediaMTX, WebRTC/MJPEG, recording, detection events, and latency measurement. A builds mosaic, focus, detector, attention promotion, and confirmation in the webcam console. B completes the language resolvers after hardware bring-up permits, then returns to hardware acceptance or sim hardening.
- **Phase 5, language completion (Sept 15 to 19):** C expands the eval to the full cached 200-item set, adds the local fallback, and closes compiler failures. B completes resolver edge cases. A finishes Web Speech and preview/confirmation polish. If the Band direct-access gate passed, A and C complete its mapping, conformance, and hardware acceptance. All three complete the utterance set, responder review, and real-drone language demonstration when the hardware gate is open.
- **Phase 6, hardening (Sept 19 to 24):** B runs failure drills; C runs adversarial tests and cuts the release; A produces the demo reel and documentation for the webcam console, language flow, video, and any Neural Band integration supported by real access evidence.

## Dependency and capacity stress test

1. **Phase 1 is a hard gate.** Language needs the frozen intent schema, authoritative relay state, planner, arbiter, sim, and console. Starting before the Sept 4 exit would make C build the compiler against moving contracts.
2. **C sets the early-language limit.** Moving video work to Sept 10 creates room for a frontier compiler, validation, logging, cached CI, and a provisional set. The full 200-case eval, local fallback, speech work, and hardware support require the Sept 15 to 19 completion window.
3. **B cannot own two safety-critical lanes at once.** Hardware bring-up takes precedence whenever equipment arrives. The early language slice therefore uses current selection, explicit IDs, and existing mission intents. Full resolver semantics stay with B and land after bring-up capacity opens.
4. **A's freed glasses time is reassigned without assuming Band access.** A moves preview, confirmation, clarification, and the Band access check into Sept 5 to 9, then owns the existing video/perception UI work from Sept 10 to 15. If direct Band access is evidenced, its half-day to one-day mapping task uses remaining Phase 2 or Phase 5 capacity. No replacement glasses feature fills the schedule.
5. **The producer boundary is ready, but device access is not.** Once a direct Band event source exists, the source-specific work is event-to-intent mapping, native tests, registration data, and CI coverage. Relay routing, planner lowering, and arbitration remain shared. Meta's documented public path currently puts those input events inside a glasses-hosted Web App, so the shared console boundary alone does not make the integration feasible.
6. **Hardware support consumes A and C time.** Flight blocks need B plus A or C. They should be booked as bounded blocks. Unscheduled acceptance work would delay A's language/video work or C's compiler/media work.
7. **Language does not depend on video or perception.** Its inputs are text, authoritative state, Intent v1, and deterministic resolvers. Moving language ahead of Phase 3 removes the original A/C overlap between compiler/UI work and MediaMTX/detector work.
8. **Real-drone language acceptance depends on two gates.** The Phase 2 compiler path must be green on sim, and the hardware adapter/positioning path must be green. The sim exit can occur first; the real-drone claim waits.
9. **Band hardware and API access are dependencies.** The console seam and synthetic mapping fixtures can land early, but they do not prove integration. The Band claim requires a direct event API independent of the glasses-hosted bridge, a real producer, and hardware acceptance. If access or hardware is unavailable, the Band remains a named capstone blocker while webcam and language continue as demo inputs.
10. **Sept 24 hardware claims depend on arrival.** A software release can still ship on Sept 24. Six-drone reliability, six-source video, and five consecutive hardware missions require their recorded hardware evidence.

## Glasses-cut verification

The cut removes an input/display client and its deployment work. The relay remains source-agnostic, the planner still accepts Intent v1, the arbiter still validates every intent and command, and adapters remain unchanged. A future direct Band producer can join the webcam console before the relay without changing that back end. Meta's currently documented public path still depends on a glasses-hosted client, so this draft marks Band access blocked instead of replacing the deleted client with an unapproved service.

When the approved PRD is edited after this review, stale glasses references will need mechanical removal from the summary, goals, architecture diagram and component table, verification text, §5.9, the Neural Handwriting sentence in §5.10, failure degradation, release/deployment text, roles, risks, Appendix A's source example, and Appendix D's `glasses/` directory. Those are consistency edits required by the fixed cut; they do not alter the resequenced scope or any back-end contract.
