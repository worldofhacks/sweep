# Feature completion, review and merge handoff

Updated 2026-09-05 after Koby's 18:50 UTC scope clarification. The GitHub status table was read at 18:40 UTC; PR #140 was created afterward. Refresh remote state before acting.

## Assignment and outcome

The incoming agent owns completing the features represented by these PRs, resolving integration and correctness defects, validating the finished workflows, obtaining independent review, and merging the resulting heads. This includes substantive repairs. Koby's earlier request for the outgoing team to make only quick final commits does **not** limit the incoming agent's completion scope.

This document is the implementation handoff, not an approval or a claim that the features are done. Treat existing code as a starting point. Preserve useful work, reproduce reported failures, repair the underlying behavior, and exercise the production path. Tests that merely restate fixtures do not establish feature correctness.

Start with #102 and #46, which unblock most work. #102 needs verification of its main-update delta; #46 needs independent verification of its repaired asynchronous execution. #121 then integrates relay rollback with #46. Map geometry and localization must adopt #102's repaired artifact contracts. The C1 stack still needs its published language, altitude and capability repairs. Console and bridge PRs require integration with the shipped shell and current relay contracts.

Read the repository's applicable AGENTS.md files, [delivery plan](https://github.com/worldofhacks/sweep/blob/main/docs/mvp-plan.md), [PRD](https://github.com/worldofhacks/sweep/blob/main/docs/prd.md), and the relevant package README before editing. Current main includes the new console shell (#114), transcript compiler (#41), and calibration tools (#103). Do not restore old App.tsx layouts or overwrite current safety behavior while resolving conflicts.

Software merge and physical acceptance are separate outcomes. The first checkpoint is the bounded two-drone workflow; the full MVP includes 4–6 drones, webcam and spoken control, and camera/telemetry/sensor console behavior. Simulator, mocked-provider and synthetic-video results cannot certify hardware flight or surveyed localization. Preserve those limitations in the final report.

## First actions and integration order

1. Fetch origin and refresh PR heads, bases, CI and review bodies. Main at the snapshot was `e043035d8cefe956da9997f195888cb59d629782`.
2. Claim implementation ownership for each active change so outgoing sessions do not edit it concurrently. Former contacts: dev owns #46/#49/#68; dev-2 owns #121/#47; dev-4 owns mapping/localization and #140. #109–#113 came from a separate C1 research session. This handoff transfers completion work when the incoming agent takes it; local owner notes are supplementary, not required access.
3. Core integration order: #46 → #121 → #47 → #49 → #111 → #110 → #109 → #112 → #113. Map order: #102 → #106 → #105 → #132. #140 is the isolated #139 fix and follows #102 or must be correctly retargeted after it. Work can proceed in parallel where files and contracts permit; serialize merges that change another candidate's base.
4. Before final review, include current main and the actual merged dependency. A PR clean against an old feature branch is not proven clean against main. After squash merges, replay only the dependent PR's own commits; inspect range-diff and review additional integration commits explicitly.
5. Require successful current-head CI and an explicit approval naming the resulting SHA. GitHub has reassociated old review commit IDs during update-branch; read the approval body and timing, not just the APPROVED badge or commit_id. Supply the exact SHA to the merge API and stop if it changes.

Historical review policy capped repeated reviews at published fixes and fix-caused regressions, with unrelated findings filed as follow-ups. #139 was explicitly deferred under that policy. Keep findings visible; the cap is not evidence that a defect disappeared. The incoming reviewer should retain the recorded dispositions while completing the features and report any changed acceptance decision to Koby.

## Published inventory

GitHub merge state is relative to the declared base. CI below belongs to the listed head. These are historical observations, not merge authorization.

| PR | Head | Base | GitHub merge state | CI | Draft |
|---|---|---|---|---|---|
| #46 | eb0328cefb95abc682b9d1af157d1990a2c51b9e | main | behind | python: success, m14 browser path: success, console: success | False |
| #47 | c90dcc023cfa67ef401141caf77264fc098f0123 | main | dirty | console: success, python: success | False |
| #49 | 74edd67853721389c11b886e2dc41439b8cd1c19 | main | dirty | console: success, python: success | False |
| #68 | 3b7b7cd8fe7a87b0769da920468fb2d9f8e6dbc8 | main | dirty | none | True |
| #102 | 61946e6d671d09a00bf4ce864bc559b685435316 | main | clean | python: success, console: success | False |
| #105 | 9f2267c7fcab2fa68c69787c6717f9ac25a5fe89 | agent/map-survey-bundle | dirty | console: success, python: success | False |
| #106 | 92bd919ec48556c48a029dc1c3f55370d2eda273 | agent/map-survey-bundle | dirty | python: success, console: success | False |
| #107 | 3f6750b58296b601e82acd3914266555c831c5f5 | main | behind | python: success, bridge-jvm: success, console: success | False |
| #108 | d5284b5218b9d86eaab37d84af940abdc81c2549 | main | dirty | python: failure, console: success | False |
| #109 | 6d6723704a64096e386daf108fe6fe1a474a07d5 | feat/issue-37-transcript-compiler | dirty | python: success, console: success | False |
| #110 | 466459e5df3ebef03cebb5f20ee2def58309a1bb | feat/issue-37-transcript-compiler | dirty | python: success, console: success | False |
| #111 | 592df471eb87497de5cdd100b853df6b1c3032c0 | feat/issue-37-transcript-compiler | dirty | python: success, console: success | False |
| #112 | b1d6ced42dc9e9dc11cedb1eb669a11ff781503b | agent/c1-capability-profile | clean | console: success, python: success | False |
| #113 | 6ae47dfea16f6b87642d5f53f944314cda7f06b6 | agent/c1-capability-altitude | clean | python: success, console: success | False |
| #121 | c35727dc72c66a02b7fdd158c17be3a2ff9020b9 | main | behind | python: success, console: success, m14 browser path: success | False |
| #125 | 3237b75d6942efdca2e242632d675c6d6eaba1ae | main | behind | python: success, console: success | False |
| #127 | 78a507b6d916bb3ae44b86a0bbc0dde3df154d26 | feat/issue-43-pilot-app-skeleton | clean | bridge-jvm: success, python: success, console: success | False |
| #132 | b9a8169478fbcc3c800c3bcf45d6b5732ff23295 | agent/offline-tag-localization | clean | python: success, console: success | True |

| #140 | 7a59f96137b90c24a33b103e338e237eca386179 | agent/map-survey-bundle | Created after snapshot; refresh | Owner reports 1,013 Python passes; remote CI not checked | False |

## Core control and relay

### #46: complete the production control workflow

[PR #46](https://github.com/worldofhacks/sweep/pull/46), remote branch `feat/issue-17-button-sim-gate`, candidate `eb0328cefb95abc682b9d1af157d1990a2c51b9e`. This replacement is published with green Python, console and browser CI at the snapshot. Owner reports 914 Python and 409 console tests passing. Latest submitted defect verdict was on the previous head; independently verify the replacement rather than repairing already-fixed code again.

The required structure is three-phase ACK handling in RelayRuntime: validate/mutate/publish under `_session_operation`, perform adapter I/O outside that operation, then re-enter it to validate ownership, commit and publish. Nonterminal and malformed ACKs remain in the ordered operation. The asyncio operation orders state publication; the session RLock alone does not.

Verify an ownership token at every dispatcher command boundary and recovery-HOVER boundary. E-stop retirement invalidates the token. Lost ownership must produce a benign discarded result, with no renewed GOTO or recovery HOVER; it must not throw from `_running` or `_pending_landings` and strand a durable marker. Adapter implementations must enforce the explicit E-stop latch contract even if a thread is preempted between validity checking and I/O. Verify both latch behavior and the distinction between a refused adapter invocation and actual motion; do not report one as the other.

Required public-path regressions:

- Interleave E-stop after phase-one publication, after validation before I/O, between resumed commands, and after I/O before commit. Check the command trace, continued session usability, monotonic state_sequence, and agreement between live events and replay.
- E-stop must remain responsive while resumed GOTO I/O is blocked. Late completion must not revive retired ownership.
- Delivered, confirmed selected LAND must survive a later HOLD/E-stop reservation whose acceptance delivery fails. Failed selected-LAND retry must reopen confirmation.
- Failed acceptance delivery must release prepared execution. Include initial and buffered ACKs, malformed/nonterminal ACKs, and activation backlog ordering.
- Preserve immediate HOLD → COME_HOME and ESTOP → confirmed LAND_ALL without the old 501 ms sleeps. Older/equal-time recovery and unsafe motion suppression remain enforced.

Run the real browser → relay → two-simulator mission, including geofence refusal before motion, link-loss behavior, selected LAND and E-stop. App remains the shell; controls live in `console/src/modules/control/ControlModule.tsx`. If the candidate passes review, integrate current main and verify the final merge delta and CI on the new SHA.

Sources: [last concurrency verdict](https://github.com/worldofhacks/sweep/pull/46#pullrequestreview-5120742138), [previous integration findings](https://github.com/worldofhacks/sweep/pull/46#pullrequestreview-5120619721). The replacement implements the subsequent four-part design; independent acceptance remains required.

### #121: integrate transactional relay recovery

[PR #121](https://github.com/worldofhacks/sweep/pull/121), branch `fix/live-relay-69-70-71`, `c35727dc72c66a02b7fdd158c17be3a2ff9020b9`, two commits above #46's eb0328ce. Its main-based diff currently includes #46. Replay its own repair after #46 merges, preserving unlocked adapter execution and the ordered phases.

Complete and verify the issues [#69](https://github.com/worldofhacks/sweep/issues/69), [#70](https://github.com/worldofhacks/sweep/issues/70), and [#71](https://github.com/worldofhacks/sweep/issues/71): ambiguous retries cannot duplicate sink effects through direct/chained/malformed/stale aliases; outbound queues are bounded; failed or slow senders terminate receiving and resolve delivery receipts; audit failures restore state while preserving the session fence.

Integration coverage must include controller-generated HOLD/E-stop admission, shared ACK receipt, failed prepare marker, failed completion commit and failed continuation commit. Queue aliases restore in place; ownership and continuation bookkeeping restore with the journal. Check every #46 concurrency boundary alongside disk-full/audit begin-and-commit failures. Avoid a full-history copy per telemetry tick. Owner reports 968 Python, 409 console and 74 focused passes plus a first-run browser mission. Repeat full integrated gates after the actual base merge.

### #47: finish full simulated mission controls

[PR #47](https://github.com/worldofhacks/sweep/pull/47) has conflicts and no submitted review. Move its altitude, formation, spacing and sweep controls/tests from App into ControlModule. Integrate `issueM15Intent` and `prepareSweep` with current hooks, selection, confirmation and projection. Preserve main's delivery plan and PRD during document conflicts.

Reconcile `arbiter/safety.py` command permissions, formation and spacing additions with current safety checks. Exercise the full scripted mission through the real console/relay/planner/arbiter/simulator, including unsupported capabilities, unsafe spacing/altitude, stale telemetry, cancellation and E-stop during execution. Coordinate overlap with #110 altitude and #111 capability profiles so the finished system has one consistent admission contract. Old green CI does not cover the merged shell or repaired relay.

### #49: finish voice transcription integration

[PR #49](https://github.com/worldofhacks/sweep/pull/49) has older approval but conflicts. Keep `relay/voice.py`, `relay/voice_telemetry.py`, transcription endpoint and tests. Drop its old console patch during integration because main already contains the ported voice client and Speech module. Verify behavior rather than assuming identical file names imply identical handling.

Preserve mixed-stream WebM audio selection, bounded transient retries, single-attempt permanent/decoding failures, typed `transcription_unavailable`, and zero emission on failed or prematurely ended recording. Verify browser microphone capture through the actual transcription provider. Wire the Speech module to the real compiler endpoint after this PR lands; this was previously a nonblocking follow-up, but remains feature-completion work for the incoming agent. Test transcription → preview → confirmation → dispatch, with refusal and cancellation.

Source: [voice approval and exception contract](https://github.com/worldofhacks/sweep/pull/49#pullrequestreview-5117508181).

## Mapping, calibration and localization

### #102 and #140: finish artifact integrity

[PR #102](https://github.com/worldofhacks/sweep/pull/102) is at updated head `61946e6d671d09a00bf4ce864bc559b685435316`, clean with green checks at the snapshot. Review 5122039553 has been reassociated with this SHA, but its body explicitly approves `7ab8b98f`; verify the merge-main delta and obtain an explicit new-head verdict. Include the original #102 commit in comparison: a range starting at `131217e3` excludes that commit.

Preserve canonical Tag 0, registration/source-transform provenance, immutable version-to-content-digest acceptance, exclusion of forbidden topology through aliases, retained hashed document snapshots, path confinement and non-destructive extractor outputs. These contracts changed during repair and every downstream consumer must adopt them.

[PR #140](https://github.com/worldofhacks/sweep/pull/140) is the newly published isolated [#139](https://github.com/worldofhacks/sweep/issues/139) fix at `7a59f96137b90c24a33b103e338e237eca386179`, branch `fix/issue-139-unicode-paths`, based on #102. It normalizes NFC before casefold and includes portable absent composed/decomposed filename collision tests. Owner reports 1,013 Python passes and both reproduced cases failing before the fix. Review and merge this small PR after the base integration; keep its two-file scope. The Tag 0 floor check was included in #102 and tracked as [#138](https://github.com/worldofhacks/sweep/issues/138).

### #106: adapt geometry authoring to validated snapshots

[PR #106](https://github.com/worldofhacks/sweep/pull/106) owns exactly two commits in `131217e3..92bd919e`: `fbc1a5a` and `92bd919e`. Replay those two after #102's actual squash merge, compare the original range, then make a separate compatibility commit for the new version-to-digest registry and retained zones/obstacles/tags snapshots. Do not reopen validated files for geometry generation.

Preserve the fixed ID-based atrium preview in either formation order and rejection of zero-length routes while allowing repeated waypoints on traveling routes. Validate generation and preview through the CLI against a repaired real-shaped bundle. Review the compatibility delta on the final head. Keep [#134](https://github.com/worldofhacks/sweep/issues/134), [#135](https://github.com/worldofhacks/sweep/issues/135), [#136](https://github.com/worldofhacks/sweep/issues/136), and [#137](https://github.com/worldofhacks/sweep/issues/137) visible as deferred edge cases; triage them as part of finishing the tool.

### #105: repair pose estimation and replay before use

[PR #105](https://github.com/worldofhacks/sweep/pull/105) is still the old `9f2267c7` head. Its seven preflight findings were reproduced, not submitted as a formal review on the stale commit. Integrate after #102/#106, retain #103's NumPy/OpenCV dependency pins, and fix:

1. TagLocalizer must consume retained validated tag/source bytes. A swap after validation changed a tag by 10 m under the original map hash.
2. Parse calibration from the same bytes that were hashed. Eliminate hash-then-reopen races.
3. Enforce the producer's calibration acceptance contract from merged #103: focal uncertainty, independent FOV bounds, finite/plausible intrinsics and distortion, and required pipeline metadata. Reject malformed claimed-live artifacts before pose estimation.
4. Enforce documented observed tag-size and incidence gates. Low reprojection RMS alone admitted a grossly wrong pose in the actual detector.
5. Track uncovered propagation intervals. A fresh velocity sample at t=0.4 must not conceal a missing t=0.2–0.4 interval after the t=0 fix/sample.
6. Copy/validate caller-owned NumPy arrays at admission. Mutating a prior velocity to NaN must not produce accepted NaN position.
7. Normalize malformed pipeline errors into the CLI's typed JSON refusal; update its documented example to #103's current fields.

Use the real detector regression, not an injected PnP result. Existing one-tag fixture: tag36h11 ID 0, size 0.3 m, identity map transform, corners TL/TR/BR/BL `[-.15,.15,0],[.15,.15,0],[.15,-.15,0],[-.15,-.15,0]`; K has fx=fy=900, cx=640, cy=360, zero distortion. Render a 480px marker with source corners `[0,0],[479,0],[479,479],[0,479]` into a white 1280×720 image using these destination corners:

```python
[[746.9736327854403, 354.398952411021],
 [781.9631754026562, 354.19551065601235],
 [781.6222411083165, 370.72500699795324],
 [747.5444841779671, 369.5365296999534]]
```

Call the actual localizer estimate path. On OpenCV 4.14.0 / NumPy 2.5.2, both reviewers/owner reproduced acceptance with about 14.19 m translation and 2.18 rad rotation error despite about 0.194 px RMS. Detector-refined short edges were about 14.4–14.9 px. Assert rejection based on observed size/incidence; do not pin the wrong-pose diagnostic magnitude, which differed slightly between hosts. Fold this fixture directly into the published test so the reviewer needs no local workspace.

### #132: finish live webcam localization and collect physical evidence

[Draft #132](https://github.com/worldofhacks/sweep/pull/132) follows repaired #105. Rebase, propagate its snapshot/calibration/pose/replay fixes, and independently review the software while it remains draft. The implementation is additive under perception: decoded frames, live tag PnP, estimated capture-time correction, bounded delayed replay, constant-velocity Kalman model, freshness heartbeats and route/checkpoint reports. A webcam provides no MSDK velocity or ToF; make the prediction assumptions explicit.

Use `perception/WEBCAM_LOCALIZATION.md`. The reduced media configuration disables RTSP; the additive `perception/webcam_media.override.yml` enables loopback RTSP without authentication. A real MediaMTX 1.20.1 synthetic-marker smoke produced 14 accepted observations and aged red after publisher shutdown. That establishes transport behavior only.

Physical acceptance requires Koby's calibrated 1280×720 webcam, decoder-specific measured latency, pinned surveyed map and camera mounting, a full hand-carried route recording/JSONL with no localization gap over 500 ms, and at least six independent held-out checkpoints. Keep the PR draft until that evidence exists. Later arbiter HOLD/LAND response to localization loss must be integrated and tested; observation-only output does not establish fleet spacing or flight approval.

## C1 language, altitude and capabilities

All five published C1 PRs still carry requested changes. Integrate with main's merged #41 before deciding which reports remain reproducible; some shared compiler fixes may already exist there. Preserve existing corrections rather than replaying stale semantics over them. Merge profiles before altitude/language integrations; #112/#113 must contain the repaired dependencies.

### #111: immutable, end-to-end capability contract

[Review](https://github.com/worldofhacks/sweep/pull/111#pullrequestreview-5118855359). Normalize or reject capability collections so they are immutable enum-only values. Regress mutation of the original set and invalid string members. Bind the session and actual router/planner profile even when the sink is `router.__call__`; reject or safely update planner reconfiguration after construction. Console decoding must reject empty, duplicate, unknown and contradictory advertisements, retain capability state, and prevent disabled non-safety emissions. Cover decoding → retained state → actual attempted emission.

### #110: validate current altitude geometry through completion

[Review](https://github.com/worldofhacks/sweep/pull/110#pullrequestreview-5118887020). Fresh measured peer positions must override stale completed projections during runtime admission. Regress sequential ascent after the upper aircraft drifts into the lower aircraft's path. Check attained geometry at final synchronous and asynchronous HOVER completion against ceiling, surveyed floor, geofence and spacing. An unrelated JOIN at final ACK must not skip a simultaneous grounding/configuration change; reproduce grounding version v1→v2 at that boundary. Preserve stale/configuration refusal and stop ownership.

### #109: bind horizontal speech to physical motion

[Grammar finding](https://github.com/worldofhacks/sweep/pull/109#pullrequestreview-5118824681), [axis finding](https://github.com/worldofhacks/sweep/pull/109#pullrequestreview-5118837621), [numeric finding](https://github.com/worldofhacks/sweep/pull/109#pullrequestreview-5118844522). Surrounding words, multiple clauses and punctuated named selections must not bypass deterministic grounding. Regress “please fly forward 5 feet” and “drones one, and two fly forward 2 feet” with deliberately wrong model output through preview and dispatch. A grammar miss must refuse or clarify.

Cover accepted Fly/Move/Go vocabulary. Aircraft-relative +dx means forward, +dy means left. Use independently derived world targets at distinct headings for all four directions. Reconcile the corpus, prompt and README with the one-foot unstated-distance contract and step_m conversion. Oversized integers must return typed invalid_model_output; also preserve #41's derived scale/rotation/target-addition overflow refusals with zero flight calls. Do not validate fixture labels against themselves.

### #112: activate effective profiles through the shipped runtime

[Review](https://github.com/worldofhacks/sweep/pull/112#pullrequestreview-5118895709). RelayRuntime must construct sessions with the actual altitude-enabled profile and keep advertised/admitted capabilities aligned during planner reconfiguration. Test startup, advertisement, altitude admission and reload through the shipped runtime. Manual session/router composition cannot substitute for this test. Carry all #110/#111 repairs into this integration.

### #113: finish provider-to-altitude execution

[Review](https://github.com/worldofhacks/sweep/pull/113#pullrequestreview-5118941484). Add height_m to the actual strict Anthropic tool schema; its additionalProperties:false currently prevents valid absolute-height output. Test the outgoing schema and obtain provider evidence for “hover at 5 feet.” Bind prefixes/suffixes, zero/multiple matches, height and named selection; wrong height 3 m/selection[1,2] for “drone one hover at 5 feet” must not execute. Terminal altitude HOVER requires appropriate hover state, not merely matching position marked airborne. Persisted oversized altitude steps must become typed invalid audit records rather than OverflowError. Verify ordered-plan advancement only after valid completion. Include repaired #109–#112 dependencies and full production-path tests.

The merged #41 release-2 provider artifact had independently verified 50/50 replay with 48 recorded responses, source/corpus/prompt/cassette bindings. Preserve it as evidence for its original source. Changed prompts, schemas or corpus require correctly bound new evidence; old replay scores do not establish new live-provider behavior.

## Media, bridge and remaining PRs

### #68 replacement: real stream in the current console

Original [#68](https://github.com/worldofhacks/sweep/pull/68) is a conflicting draft. Use published `fix/pr68-console-demo` at `f817fd1b6919bb8024a336ec65ad0cab0d292e08`, integrate current Control/Gesture/Live modules and `console/src/media/runtime.ts`, open a replacement PR, then close the original with its link. Do not overwrite main with superseded LiveMedia/runtime-config files.

Demo scope is MediaMTX → WHIP test source → WHEP playback in Live. Authentication and recording were explicitly deferred; keep auth/retention follow-ups rather than silently expanding the demo. Existing evidence shows advancing 640×360 H.264 frames, cleanup on navigation and reopening; control state was a fixture. Reproduce in the actual integrated shell, including stream failure/reconnect and WHEP session cleanup. Prove the selected live-feed workflow alongside real control state before claiming the complete feature.

### #107/#108/#127: finish the remote bridge with explicit hardware limits

These PRs were previously parked or unreviewed; the new completion handoff must account for them. Read their [#43](https://github.com/worldofhacks/sweep/issues/43) plan and PR bodies before setting the remaining hardware scope. #107 provides Android pilot-app/bridge-core registration, #108 the relay command wire/RemoteBridgeAdapter/fake node, and #127 the node's relay link, stacked on #107 and dependent on #108's protocol.

First resolve #108's failing Python CI and conflicts with current relay/adapter contracts. Preserve HMAC canonical encoding, epoch/roster/sequence/TTL admission, replay protection, typed ACK failures, authenticated node state, and the rule that console fan-out exposes neither command frames nor signatures. Reconcile the new E-stop latch and #46/#121 ownership/audit rules with the remote adapter. Test backend selection through build_dispatcher and the real relay/fake-node path, including reconnect, timeout, stale epoch, forged commands and watchdog behavior.

For #107/#127 run JVM tests, Python-vector compatibility, Android fake/probe build checks where SDK tooling is available, and real on-device registration/link evidence. The published probe executor in #127 deliberately fails with control_loop_unavailable until the later control phase; do not mark hardware flight control complete from a compiling skeleton. The earlier phone session was locked, so app runtime, Wi-Fi binding, wake-lock behavior and battery-optimization flow remain unverified on-device. Koby must unlock/provide the physical device; do not request or store his PIN. Bind command/data evidence to firmware, SDK, phone and adapter versions. Indoor watchdog failsafe is land, not RTH; verify actual available authority and safe command behavior on hardware before flight acceptance.

### #125: runtime-secret documentation

[PR #125](https://github.com/worldofhacks/sweep/pull/125) is docs-only, green but behind/unreviewed at the snapshot. Verify every documented runtime variable has a current consumer, keep repository-access credentials out of application configuration, and ensure only placeholders are committed. Update main, review the small delta and merge independently when it does not invalidate another pending gate.

## Evidence and delivery checklist

For each final head, provide the source SHA and base, concise implementation delta, reproduced-before/fixed-after cases, full relevant package suites, repository-required CI, and production-path evidence. Use `just ci` if present on that head and the commands in its package docs; do not substitute a focused pass for the full package suite. Run browser scenarios against real relay/simulator services for console/autonomy changes. If hardware or live-provider access is unavailable, complete the software work and identify exactly which measured acceptance remains open.

Retain review dispositions, but inspect public behavior as well as internals. Audit failures, stale telemetry, disconnect/rejoin, delayed ACKs, malformed input and E-stop interleavings are feature requirements demonstrated by prior failures. Review shared safety/contracts independently. Merge one verified head at a time, record the merge response/SHA, update dependents, and recheck that current main is contained before the next final verdict.

Report feature completion separately from merged code. Final handoff should list merged PRs/SHAs, remaining follow-up issues, exact provider/hardware evidence, and anything still blocked on a physical input. No feature is complete solely because its PR is conflict-free or CI is green.

## Access and supporting material

This document is published on `docs/pr-review-handoff-20260905`; the incoming agent can work entirely from origin and linked GitHub issues/reviews. Local temporary logs mentioned by prior agents are historical evidence locations, not guaranteed shared files. Ask for missing evidence to be attached to the PR or committed as a suitable artifact rather than assuming a path exists on another host.

Completed work to preserve: #41 transcript compiler and release-2 evidence, #56 scheduling follow-up, #103 calibration, #114 console shell, and the current main documentation. Full Phase 1 tag print set: [150-page tag36h11 PDF, IDs 0–149](https://raw.githubusercontent.com/worldofhacks/sweep/artifacts/phase1-tags-20cm-20260905/RESEARCH/PHASE_1_TAG36H11_20CM_11X17.pdf). Black square 200 mm, white quiet zone 25 mm, 11×17in pages; print 100%/Actual Size and measure both sides. Placement still requires the surveyed plan and coverage; printing is not localization acceptance.
