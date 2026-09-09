# Requested-view contributions — 2026-09-08

Continuation of the shared Atlas design and capture workflow. This is software
and isolated-browser evidence, not physical-device qualification.

## Delivered

- Location and reconstructed-surface requests offer **Contribute this view** and
  show linked originals. The composer and Android source chooser explain the
  requested view before camera or import selection, using the existing pine/mineral,
  border-led design. No additional UI framework or dependency was introduced.
- A frozen location-cell or build/checksum/region target follows the file through
  retries, Android picker recreation, private copying and durable outbox storage.
  Both upload clients require an exact request acknowledgment as well as their
  existing upload checks. Missing or mismatched acknowledgments retain the file.
- The relay validates the request within the authorized space and commits its
  membership atomically. Retrying identical bytes reuses the original capture;
  adding a request link does not rewrite its contributor or sensor provenance.
  At most eight distinct request links attach to one original; existing links
  remain idempotent at that limit. The SQLite table is additive.
- Historical/dismissed surface links remain inspectable. Delayed contributions
  retain their original build target, without reopening or automatically closing
  requests. Linked-capture filtering happens before 24-item paging, so older
  originals remain reachable.
- Camera entry controls no longer clip at 320px. Opening capture or linked views
  clears the previous global notice, avoiding stale messages over phone controls.

## Verification

- Full console suite: **92 files / 1,250 tests passed**. Final changed Atlas suite:
  **14 files / 82 tests passed**. ESLint, TypeScript and desktop/Android web builds
  passed. Existing bundle-size and Node test-environment warnings remain.
- Full relay/spatial suite: **1,203 tests passed**. An initial sandboxed run could
  not bind loopback test sockets; the permitted rerun passed, with two existing
  Starlette warnings. Five new relay tests cover scope, exact targets, deduplication,
  quota, restart, concurrent admission, and rollback without orphan media.
- Android fake/probe: **86 / 106 unit tests passed**, both APK builds and lint
  passed. Existing lint warnings remain (26 per variant, zero errors). An initial
  lint analyzer failure was followed by a successful serial rerun; no suppression
  was added. Native tests include picker recreation, cold outbox reopen, immutable
  metadata, and missing/wrong/correct acknowledgment retries against loopback HTTP.
- Actual browser upload to a disposable Austin survey: deliberately discarded the
  first successful HTTP response, then used **Retry upload**. Two attempts produced
  one byte-identical original and one request link. Import time/GPS stayed unknown,
  observed location coverage stayed zero, and the request stayed open. The linked
  original was then opened through the UI.
- Desktop/phone requested-capture layouts passed at **1440×1000, 390×844, 320×568**.
  Dialogs fit without horizontal overflow; the camera entry remains contained and
  at least 44px tall. Maps remain visible at 250px and 170.4px on the phone sizes.
- Android bundled assets were exercised under their actual asset-origin CSP at
  **390×844 and 320×568** with simulated native IPC. Camera and import actions
  received the exact requested cell/context; no physical camera or picker opened.
- Refreshed local preview on port 8177 and verified five spaces, all 11 existing
  source captures and unchanged reconstructed-model checksum. Request-link fields
  are available without rebuilding or replacing that model.
- Ruff and Git whitespace checks passed. A final label-test edit initially used a
  Playwright-only option in Testing Library; TypeScript caught it, it was removed,
  and the build was rerun successfully.

Screenshots are local under `output/playwright/request-*`. The final 320px camera
entry, 390px request card and native source chooser were visually inspected.
The isolated fleet-free browser also logs `map_unavailable` responses from
navigation polling: it has no approved robot world-map bundle. Media playback is
unconfigured there. Those responses are not counted as successful provider checks.

## Limits

Request membership is not proof of GPS coverage, complete reconstructed surfaces,
safe access, or a safe robot route. Imports do not acquire coordinates from a
request. Contributor push notifications are not implemented. Physical Android
capture/background-upload and live provider qualification remain outstanding.
Reconstruction licensing, geographic registration and broader Atlas goal limits
are unchanged. No physical robot/camera/microphone action or public repository
publication was performed for this increment.
