# Private space drafts and exactly-once publication — 2026-09-08

This is verified progress toward the full Atlas objective, not full-product completion.
The preceding goal turn completed and recorded UI/native-import verification. This turn
closes the implemented-new-space draft gap without changing fleet controls or reconstruction.

## Implementation

- One local draft per exact workspace credential: incomplete text, category, coordinates,
  radius, draft ID, revision, and immutable pending-publication payload. No new dependency,
  UI framework, background publisher, or broad Android permission.
- Desktop: bounded JSON in local storage, hashed credential scope (no raw token persisted),
  [Web Locks](https://www.w3.org/TR/web-locks/) plus revision checks for cross-tab writes.
  This is local browser storage, not encryption or a cold-offline website installation.
- Android: existing encrypted vault, original-credential checks, bounded input and native
  synchronous commit acknowledgment. Lost local replies can retry identical writes/removals.
  Scoped contribution invitations cannot read/write these owner drafts or publish a space.
- Relay: additive `published_drafts` table; transaction serializes draft lookup and space
  creation across separate SQLite connections. Same key/content returns the same space,
  including after restart or at workspace capacity. Changed content is refused. Replays
  return no invitation secret, never rotate access, and preserve current resolved status.
- Publication freezes and saves the original report before HTTP. Failed or missing replies
  keep a durable retry identity. No fallback to non-idempotent creation. Local discard is
  explicitly confirmed and does not delete any shared report or source capture.

## Verification

- Full console: **91 files / 1,243 tests passed**, build and ESLint passed. After the final
  notice-layout and offline-copy corrections, **all 75 Atlas tests** and full lint passed.
- Full relay/spatial: **1,198 tests passed** with two existing Starlette deprecations.
  Five new HTTP/store tests cover lost replies/restart, invitation preservation, changed
  payload refusal, owner scope/validation, parallel connections, and retry at capacity.
- Both Android APKs assemble; **83 fake / 103 probe tests pass**, including five native
  draft tests. Both lint variants pass with **26 pre-existing warnings** each. The local
  `UseKtx` style suggestion is narrowly suppressed because its suggested helper discards
  the commit success result required to label a draft saved; no runtime failure is hidden.
- Actual built desktop UI with real isolated HTTP/SQLite: offline API failures, preserved
  text/location after reload, then a deliberately dropped **201 response after commit**.
  Two publication attempts returned one space, with identical payload and draft ID.
  Initial directory: one demo; final directory: two demos; **zero duplicate spaces**.
  Draft `ac72740a-71fc-4cd2-8098-4992f1d8a11e` published space
  `84fc99e3-bd1b-403d-a6a0-819aec6b91cc` only in the isolated audit store.
- Eight desktop/phone layout observations passed at 1440×1000, 390×844, and 320×568:
  editing, action area, uncertain publication and confirmation. Maps stayed visible at
  250px (390px phone) and 170.4px (320px phone); no horizontal overflow or navigation overlap.
- Two real browser windows read the same draft; a newer edit was preserved and the stale
  editor was refused. Canceling discard kept the draft; explicit discard removed only the
  test draft. This audit found an old success toast covering phone draft controls. Creation
  notices now live inside the form and starting a draft clears stale notices; rerun passed.
- Built Android assets under shipped CSP use simulated native IPC for UI evidence, with
  real read-only HTTP to the isolated workspace. Drafts survived Uploads/Spaces navigation
  and offline reload without using web draft storage or invoking native capture/publication.
  Four final offline layout observations passed at 390×844 and 320×568. All three draft
  actions fit together on screen while the map and bottom navigation remain visible.
  The offline status copy was shortened to preserve working space on the short phone.
  This complements, but does not replace, Kotlin storage tests and physical-device testing.
  JVM draft tests use a SharedPreferences test store; physical Keystore behavior was not exercised.
- The user preview was refreshed while retaining its credential in memory and its existing
  workspace/data. The existing 11-source, 98,951-triangle model remains ready with unchanged
  SHA-256 `e1ea6aa6c8f16a47e5ad83f9eaafa6e14b4cc6b632c26d5e879bfe1669b9f9b7`.

Screenshots are local under `output/playwright/draft-*`. The only discarded data was the
new, explicitly named isolated conflict-test draft; it was not a user draft and cannot be
restored. Existing spaces, original captures, other local drafts and reconstruction were
not deleted. No public incident, GitHub change, push or merge was made this turn.

## Remaining acceptance boundaries

ADB reported no attached Android device. No physical camera/microphone or robot operation
was performed, and no emulator license was accepted. Actual Android permission/lifecycle,
Keystore/disk-failure/cold-start behavior and second-device contribution still need testing.
The full goal also retains production reconstruction licensing/georeferencing/accuracy,
qualified missing-surface guidance, proactive notifications and other ledger requirements.

Drafts are local to a browser profile or app installation and exact workspace credential.
They are not shared, not moved automatically on credential rotation, not emergency-service
notifications, and not retained after clearing local storage. Server deployment must precede
client rollout. Until a publication reply is confirmed, users retry the frozen report;
discarding its local draft never retracts a report that may already have been published.
