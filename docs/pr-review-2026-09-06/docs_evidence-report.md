# Documentation and evidence review

Reviewed the supplied heads for #181, #204, #224, #225, #226, and #230. The documentation accurately distinguishes software implementation and rehearsal from physical flight acceptance. No unresolved documentation or evidence defect was confirmed.

## Evidence boundary

The acceptance evaluator in #226 is an offline verifier for recorded five-route evidence. It validates manifests, raw-evidence hashes, independent-reference declarations, pairings, update coverage, per-route and aggregate p95 error, and the configured limits. Its tests use synthetic fixtures to exercise that verifier. No physical recording package, independent reference series, or completed acceptance report was supplied, so this review records no localization-accuracy, dropout, or physical-flight result.

The README and the runtime-wiring audit make the same boundary explicit. They describe the navigation/search exercises as simulated software integration and retain the required measured timing, transforms, localization, failure drills, RC takeover, and five-route acceptance gates. The historical integration revision named by the runtime audit, `7d11aa4`, is present locally. Its stated aggregate test and browser-rehearsal results are historical evidence claims; this review did not rerun that full integration checkout.

## Per-head review

| PR | Head | Reviewed boundary | Result |
| --- | --- | --- | --- |
| #181 | `8b78204e` | audit durability, mirror recovery, sampled derived state, telemetry retention, console truncation and capture-bundle bounds | Fixed during review. The forced update exposed an unbounded capture-bundle media list that could exceed the audit-record cap; the reviewed head limits it to 64 records. Canonical telemetry remains durable and unrecoverable divergence fails closed. |
| #204 | `903d0d1d` | live telemetry fan-out versus durable audit replay after reopen | Clean. It removes only the duplicate fleet-state snapshot persistence; telemetry events remain part of ordered replay. |
| #224 | `1966a5e9` | historical runtime-wiring record and its software/physical scope labels | Clean. The audit identifies software rehearsal as such and retains the physical gates. |
| #225 | `dea2dc59` | README, PRD, and MVP-plan scope, status, and runnable commands | Clean. `justfile` implements the documented setup, test, lint, CI, relay, and console commands; browser command matches `console/package.json`. Local Markdown links resolve. |
| #226 | `7abf28fa` | manifest/evidence parsing, integrity binding, acceptance thresholds, report output | Clean. The evaluator fails malformed, mismatched, or incomplete evidence and cannot turn synthetic test data into a physical acceptance claim. |
| #230 | `115e6025` | upload deadline documentation, setting contract, and no-emission timeout path | Clean. The route documentation describes a total streamed-body deadline, matching the setting and tested route behavior. |

## Traced contracts

- #181: `RelaySession` projection updates feed `SessionAuditLog.append_batch`; operations retain sequence, digest, length, and only the current recovery lines. Reopen verification validates the mirror before replay. Capture-bundle media has a 64-record wire bound before it reaches the audit. The separate #177 integration follow-up must still bound aggregate capture projection size.
- #204: accepted telemetry updates live subscribers and writes its telemetry audit event. The redundant `state` projection write is absent; reopening reconstructs the latest telemetry through ordered audit replay.
- #226: evidence directory and manifest feed `evaluate`; parsers construct typed samples, pairing/gap checks derive route metrics, and `_validate_hashes` binds report inputs before `_write_report` publishes a result.
- #230: authenticated transcript request feeds `_bounded_request_body`; expiration maps to typed `upload_timeout` before decoding, provider transcription, or compilation.

## Validation

- `uv run pytest -q tests/test_flight_acceptance.py`: 15 passed.
- `uv run python -m evals.flight_acceptance --help`: passed.
- `uv run pytest -q relay/tests/test_transcript_upload_deadline.py relay/tests/test_voice.py`: 44 passed.
- #181 reviewed head: 199 focused audit and contract tests passed; Ruff and diff checks passed. Hosted CI run `34020975915` passed Python, console, bridge JVM, Android unit, and M14 browser-path jobs.
- `git diff --check` passed for every reviewed range.
- Changed #225 local Markdown links resolve.

The pytest commands emitted the known cleanup warnings for protected MediaMTX recording fixtures; all selected tests passed.
