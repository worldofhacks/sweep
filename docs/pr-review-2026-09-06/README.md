# PR audit ledger

This ledger records 53 original pull-request audits, #231 through #234, and #235 detection attention. `coverage.json` is the machine-readable index: it records each original PR's exact base and head, its coverage form, checked symbols or calls, result, and source ledger.

The audit began from `d83fda0180ee02d80a9a75f82bb2345fcdcf0f41`. Main remained at that head during this work. The final Python run passed 2,424 tests; the console run passed 531 tests, lint, and the production build.

## Coverage

| Slice | PRs | Coverage form | Ledger |
| --- | ---: | --- | --- |
| Navigation | 12 | Symbol groups and caller or consumer chains | `navigation-coverage.json` |
| Phone and media | 8 | Individual functions or methods | `phone_media-coverage.json` |
| Console and demos | 11 | Symbol groups and runtime or UI call paths | `console_demo-coverage.json` |
| Documentation and evidence | 6 | Symbol groups and evidence boundaries | `docs_evidence-coverage.json` |
| Search | 16 | Symbol groups and mission call paths | `search-coverage.json` |

The 53 original PRs are #164, #173 through #177, #179 through #181, #183 through #188, #190, #193 through #196, #198 through #212, #214 through #226, and #227 through #230. A symbol group is a call-chain review of related symbols. It does not claim an individual review for every member.

## Follow-ups

- #231: receipt-clock correction `6cea3215e` is applied as `eecd80a`.
- #232: Deepgram parser correction `3d5258f` is applied as `006dbd8`; the current head is `9346c9b`.
- #233: closed without merge at `6e2deb1`; BVC completion truth `39a306b` is applied as `db63e99`.
- #234: open at `d8dde8b`; verified-localization corrections map to `12ecc9a` and `98c81cc`. The manifest records the independently reviewed gimbal-frame and uncertainty update.
- #235: open at `b308f92`, based on `d83fda0`. The exact head was reviewed through the configured recorded-frame processor, audit-first active-epoch deduplication, authenticated acknowledgement, and polled console state. The pinned model is YOLOX COCO `0.89728647`. Integration correction `5a0f2a7` restored the state interface, client acknowledgement method, and contract test.

## Integration fixes

The reconciliation corrected strict voice binding and language-preview identity, receipt-clock expiry, console expiry and session races, periodic presence and navigation ordering, capture audit budget, search behavior after pending cancellation and worker failure, Deepgram parser handling, and BVC actual-setpoint metadata, and the #235 attention-state interface and acknowledgement contract.

#224, #225, #228, and #233 are closed without merge. Their audit evidence remains available and closure is not recorded as integration.

## Validation

The final combined run passed 2,424 Python tests and 531 console tests. Ruff, ESLint, and the production build passed. Browser rehearsals passed for M14, fleet, navigation, and search. JVM bridge and Android fake/probe suites passed after increasing the disabled-navigation test command TTL so it reaches its authorization check under build load.

Each slice ledger contains its focused commands and results. Some focused suites ran before later reconciliation changes. Browser and phone validation remained in progress, so this ledger makes no repository-wide green claim.

## Remaining evidence

#232 remains a draft until the same 20 recorded utterances have been measured through both providers and their replay cassettes are retained. The benchmark harness exercises the real transports; mocked recordings do not establish provider accuracy or latency.

#234 remains a draft pending measured camera calibration, capture-time alignment, a producer for decoded frames and capture-aligned attitudes, and walked-camera/covered-tag checks. The raw Android exporter preserves diagnostic timing and axis conventions. It cannot establish those measurements by itself.

The integration branch contains the reviewed code from the concurrently closed #224, #225, #228, and #233. Confirm their intended disposition before choosing the final merge scope. No shared branch merge or deployment was performed by this review.

## Final CI reconciliation

The fleet browser CI audit showed a simulated node becoming stale during startup, then recovering between arm acceptance and application. The demo now uses the fake bridge's normal 10 Hz telemetry cadence. The production freshness threshold and stale-roster refusal are unchanged. Two subsequent fleet browser runs passed all 16 checks.

The revised recording helper passed 17 tests using real Docker, FFmpeg, and ffprobe. Its global lifecycle lock prevents separate recording roots from replacing or stopping the same MediaMTX service.
