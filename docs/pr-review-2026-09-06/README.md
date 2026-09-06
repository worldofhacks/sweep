# PR audit ledger

This ledger records 53 original pull-request audits, #231 through #234, #235 detection attention, and the #237 recording follow-up. `coverage.json` is the machine-readable index: it records each original PR's exact base and head, its coverage form, checked symbols or calls, result, and source ledger.

The audit began from `d83fda0180ee02d80a9a75f82bb2345fcdcf0f41`. Main remained at that head during this work. The combined Python run before the final CI corrections passed 2,424 tests; the console run passed 531 tests, lint, and the production build.

## Coverage

| Slice | PRs | Coverage form | Ledger |
| --- | ---: | --- | --- |
| Navigation | 12 | Symbol groups and caller or consumer chains | `navigation-coverage.json` |
| Phone and media | 8 | Individual functions or methods | `phone_media-coverage.json` |
| Console and demos | 11 | Symbol groups and runtime or UI call paths | `console_demo-coverage.json` |
| Documentation and evidence | 6 | Symbol groups and evidence boundaries | `docs_evidence-coverage.json` |
| Search | 16 | Symbol groups and mission call paths | `search-coverage.json` |

Exact PR numbers and reviewed heads are listed in `coverage.json`. A symbol group is a call-chain review of related symbols. It does not claim an individual review for every member.

## Follow-ups

- #231: receipt-clock correction `6cea3215e` is applied as `eecd80a`.
- #232: Deepgram parser correction `3d5258f` is applied as `006dbd8`; the current head is `2f5aba8`, including real-provider preflight cassettes and offline replay coverage.
- #233: closed without merge at `6e2deb1`; BVC completion truth `39a306b` is applied as `db63e99`.
- #234: open at `d8dde8b`; verified-localization corrections map to `12ecc9a` and `98c81cc`. The manifest records the independently reviewed gimbal-frame and uncertainty update.
- #235: open at `b308f92`, based on `d83fda0`. The exact head was reviewed through the configured recorded-frame processor, audit-first active-epoch deduplication, authenticated acknowledgement, and polled console state. The pinned model is YOLOX COCO `0.89728647`. Integration correction `5a0f2a7` restored the state interface, client acknowledgement method, and contract test.

## Integration fixes

The reconciliation corrected strict voice binding and language-preview identity, receipt-clock expiry, console expiry and session races, periodic presence and navigation ordering, capture audit budget, search behavior after pending cancellation and worker failure, Deepgram parser handling, and BVC actual-setpoint metadata, and the #235 attention-state interface and acknowledgement contract.

#224, #225, #228, and #233 are closed without merge. Their audit evidence remains available and closure is not recorded as integration.

## Validation

The combined run before the final CI corrections passed 2,424 Python tests and 531 console tests. Ruff, ESLint, and the production build passed. Browser rehearsals passed for M14, fleet, navigation, and search. JVM bridge and Android fake/probe suites passed. A later CI failure exposed a test startup race: the disabled-navigation check now waits for the link to receive the ready membership identity before issuing the command. The corrected bridge-core and bridge-node suites passed 161 and 46 tests.

Each slice ledger contains its focused commands and results. Some focused suites ran before later reconciliation changes. The final combined CI run remains pending; earlier local passes do not establish its result.

## Remaining evidence

#232 remains a draft until the same 20 recorded utterances have been measured through both providers and their benchmark cassettes are retained. A licensed human “stop” recording now has captured responses from both real providers and offline replay coverage. That one-clip preflight establishes transport operation; the full comparison remains pending.

#234 remains a draft pending measured camera calibration, capture-time alignment, a producer for decoded frames and capture-aligned attitudes, and walked-camera/covered-tag checks. The raw Android exporter preserves diagnostic timing and axis conventions. It cannot establish those measurements by itself.

The integration branch contains the reviewed code from the concurrently closed #224, #225, #228, and #233. Confirm their intended disposition before choosing the final merge scope. No shared branch merge or deployment was performed by this review.

## Final CI reconciliation

The fleet browser CI audit showed a simulated node becoming stale during startup, then recovering between arm acceptance and application. The fake node now coalesces queued periodic telemetry while preserving fresh command and membership telemetry. The browser waits for fresh telemetry from all four ready nodes with a stable roster before arming. A regression test verifies that takeoff completion follows its fresh hovering telemetry even when an older landed sample is queued. The focused Python tests and all 16 fleet browser checks passed; the full combined CI run is pending.

The revised recording helper passed 18 tests using real Docker, FFmpeg, and ffprobe. Its project/container identity locks and active-service refusal protect MediaMTX across separate recording roots. The remaining review fix is published in PR #237.
