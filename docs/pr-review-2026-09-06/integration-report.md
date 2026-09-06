# Final integration composition audit

Reviewed integration head `7aeb54187dad6e529e7bfd5eff2475c67a93041f` against `origin/main` at `d83fda0180ee02d80a9a75f82bb2345fcdcf0f41`. The configured relay starts one effective capability profile and passes it to both `AutonomyComposition` and `build_transcript_service`. The profile retains C1 and C2 release constraints, adds navigation only with a navigation deployment, and adds search only with its configured runtime. The navigation language wiring test covers C1 and C2 with search enabled and disabled.

The runtime fan-out calls `RelaySession.periodic_events()` before the autonomy session's periodic work. The autonomy session then runs the operator-presence watchdog and signed navigation-pose publisher together. The publisher records packets through the session and the app routes them only to the matching adapter. The fake-node navigation round trip completed with the frozen route, authorization, pose, and GOTO packet sequence. Mapped plans bypass BVC projection in `SafetyArbiter.filtered_goto_commands`, preserving their signed geometry. Generic BVC execution retains the actual interim setpoints and the terminal lifecycle detail. The console reducer stores that lifecycle detail in the request record and `RequestsPane` renders it.

Capture state uses `CaptureLedger` limits and the state audit accepts only its bounded projection. Provider selection accepts only `deepgram` or `whisper`; C2 remains restricted to the simulator backend. The duplicate-method AST scan found one intentional property getter/setter pair, `RelaySession.intent_sink`, and no shadowed class methods.

## Regression added

A public, authenticated navigation-preview request may carry a future client timestamp because it is a preview. The watchdog must use the relay receipt time. Commit `307358f758e9934a9ee4a9432f09b2941ab9e0c3` adds an application-level regression: it creates a ready configured session, posts a future-dated navigation preview, advances the relay clock by the watchdog timeout, and verifies that the hold safety action is produced. This covers the receipt-time behavior at the public HTTP boundary.

## Validation

`relay/tests/test_navigation_binding.py` passed: 7 tests. The broader composition command passed: 76 tests across the fake-node navigation round trip, language wiring, navigation control, audit sampling, settings, and BVC behavior. Ruff, formatting, and `git diff --check` passed for the regression change. Both pytest runs emitted the existing temporary-recording cleanup warnings.

## Standards

No confirmed standard or merge-composition violation was found in the reviewed runtime paths. The added regression uses the public endpoint and asserts the observable watchdog result rather than a private timestamp field.

## Findings

No further correction is required from this audit. The integration branch already includes the BVC completion-truth fields and terminal detail; the console consumer displays the detail through its existing lifecycle flow.
