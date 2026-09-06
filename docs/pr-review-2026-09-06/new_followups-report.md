# Follow-up PR review

Reviewed the three supplied heads against `d83fda0`. Two correctness defects were found and fixed in separate review commits. The upload deadline change is sound under the exercised stalled-body path. The benchmark runner is present, but no 20-recording human comparison was available, so this review records no provider-latency or accuracy result.

## Fixed findings

1. **Presence expiry trusted a client timestamp.** `AutonomySession.submit` in `relay/autonomy.py:480` refreshed the watchdog from `intent.t`. A timestamp inside the accepted future-skew window could extend the deadline beyond the relay receive time. The expiry comparison also waited until after the exact deadline. Commit `6cea3215e9dd95dd7470819af245d6f9afeed351` uses the relay state timestamp and expires at `>= operator_timeout_ms`. Its regression sends an accepted intent timestamped 1 second ahead and proves the hold still dispatches after the configured interval.

2. **Deepgram outcomes could not round-trip through the relay parser.** `VoiceOutcome` and the console accepted `source: "deepgram"`, while `parse_voice_outcome` in `relay/voice.py:443` rejected it. Commit `3d5258fd8510f579d163b48c5204b415f10e4eed` adds the value to the parser and covers a Deepgram wire round-trip.

## Presence and session reports: db425079c71018bafabdef77d399a38a7645799d

The console starts its one-second presence timer only after `auth.accepted`, emits the exact presence frame while visible, and clears the interval on close and unmount. The relay requires an authenticated console principal, audits each accepted frame at receipt time, then calls the autonomy sink. The watchdog records a safety action, reserves and admits a safety stop, routes it through the existing cancellation lanes, and publishes actual lifecycle and command events. Reports are built from the durable audit replay and atomically written during composition shutdown.

The fixed receipt-time defect above applies to accepted operator intents. The periodic watchdog and the existing normal/hold/estop cancellation paths were traced together. No other unresolved correctness finding was found in this head.

## Deepgram transcription: 2e1cdb4a8bd74b845f748c843c87b38f72c4d009

The configured provider reaches the normal relay startup path and the composed entry point. Deepgram posts raw bytes to the documented pre-recorded endpoint with an authorization token, `nova-3`, smart formatting, and repeated command keyterms. Provider HTTP, transport, malformed-response, and invalid-transcript failures become typed zero-emission outcomes. Provider and model data participate in recording/replay keys; the Deepgram key also contains the ordered keyterm configuration. Replay records `latency_ms: null` and does not call the network.

The benchmark runner validates exactly 20 manifest entries and real decodable files before it invokes either provider. Its test uses a generated audio fixture solely to exercise the runner. It does not constitute the requested human-recording comparison. No existing 20-clip human manifest, provider cassettes, or completed result was present, so the live benchmark remains blocked on those recordings and was not run.

The fixed parser mismatch above was the only unresolved provider-contract defect found. The Deepgram endpoint and repeated `keyterm` parameters match the current [pre-recorded API reference](https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded).

## Transcript upload deadline: 224645ffcca2dfcd16bbead4933fa075e6575c20

The transcript route applies the configured deadline around the entire streamed body read. A stalled async body is cancelled, returns HTTP 408 with `upload_timeout`, and reaches neither decoding, provider transcription, nor compilation. The byte cap still applies across fragments. The setting rejects non-positive and non-integer values and is listed in the environment contract. No unresolved correctness finding was found.

## Validation

- `uv run pytest relay/tests/test_autonomy.py relay/tests/test_session_report.py -q`: 27 passed.
- `uv run pytest relay/tests/test_voice_plan.py relay/tests/test_deepgram.py -q`: 58 passed.
- `uv run pytest relay/tests/test_deepgram.py evals/test_voice_provider_benchmark.py -q`: 42 passed.
- `uv run pytest relay/tests/test_transcript_upload_deadline.py relay/tests/test_voice.py -q`: 44 passed.

The test runs emitted pytest cleanup warnings for protected MediaMTX recording fixtures. The selected tests passed.

## Standards and hygiene

The diffs have no new ticket archaeology, source-to-test cross-references, dead branches, unused imports, or provider-specific copy/paste helper families. The new comments that remain describe timing, cancellation, or audit contracts. The report and benchmark documentation accurately distinguish replay from provider latency and do not claim a completed human benchmark.
