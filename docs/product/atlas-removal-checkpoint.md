# Source-removal checkpoint

September 9, 2026 · local implementation · LA-04 remains partial.

This checkpoint implements the removal contract, worker cleanup, web impact
confirmation, and a durable receipt directory needed by a consent-driven shared
world. It does **not** yet qualify Android removal, account deletion/recovery, or
a production backup retention policy. No user media was removed during development; tests use
disposable synthetic media and owned test processes.

## What removal means here

An authorized editor first requests an impact preview, then explicitly confirms
that version of the memory. Confirmation atomically withdraws the capture from
the Space, its gallery, timeline, coverage, original-media route, memory-context
route, recording playback, and edit/date histories. Relevant capture-response
links are removed. The original, sidecar recordings, notes, inspection, transcript,
AI context, and saved histories no longer have active database records.

Every 3D build that used the capture is invalidated, including its surface-review
requests and responses. Other original captures remain untouched. A build cannot
remain available just because it also used other people's captures. Independent
live presence, unrelated Spaces, and separately imported copies are not removed.

Withdrawal and physical cleanup are distinct:

| State | Meaning |
| --- | --- |
| `preview` | No removal; response names recording/build counts and current confirmation fingerprint |
| `cleanup_pending` | Source access withdrawn; tracked local cleanup or processing acknowledgement is unfinished |
| `local_removed` | Managed original/recording/build files are removed, tracked processing has released them, and SQLite's old WAL pages were checkpointed |

`local_removed` is not a secure-erasure, backup-erasure, or remote-download recall
claim. Browser/phone copies, recipient exports, snapshots, disk-level remanence,
and information already sent to a provider cannot be recalled by this endpoint.
No production privacy-law compliance claim is made.

## API and authority

Under `/api/sessions/{session}/atlas/spaces/{space}/captures/{capture}/removal`:

- `GET`: current impact preview, or an existing authorized removal receipt.
- `POST`: `{ "confirmation": "<fingerprint from preview>" }` performs withdrawal.
  Extra fields are rejected. A stale fingerprint returns 409 without deleting.
  Repeating confirmation after successful withdrawal returns the existing receipt.

The fingerprint covers the capture, memory revision, analysis ID, latest date
revision, recording IDs, and dependent build IDs. It is a concurrency check, not
an authentication token. Every request still requires existing Space access.

Operators retain workspace scope. Signed account contributors may remove only
their server-attributed captures; signed Space owners may remove captures in
their Space. Viewers, unrelated contributors, and legacy anonymous bearers cannot
remove captures or inspect removal receipts. Membership/ownership is rechecked
inside the withdrawal transaction. Display names do not establish ownership.

Receipts expose the capture ID, server-recorded actor and times, recording/build
counts, state, and whether analysis acknowledgement is pending. They do not expose
content, storage paths, or source digests. Responses use `Cache-Control: no-store`.

The Space-level `GET /removals` and `GET /removals/{before}` return 20 receipts
per page with an exclusive, scope-local ordinal cursor (not a global activity
counter). The archive is capped at 1,000 receipts; appends do not shift existing
ordinals within an unchanged authorization scope. Owners/operators see the Space;
signed contributors see only their own capture receipts, including owner-initiated
removals of those captures. Counts of pending/completed receipts cover the full
authorized scope, not just the current page. Viewers and anonymous legacy guests
cannot list receipts. Every page rechecks access; revocation invalidates pagination.

## Web review and status

From a Space's Captures or Timeline: Memory & sounds → Details & sources →
Review removal from this Space. The server advertises `can_remove`; old servers,
read-only users, and native clients do not offer an unsupported action. Finish or
save pending notes/recordings before opening the review. The standalone memory
library does not yet expose this action; open the source Space to remove it.

The existing dialog shell, warm ivory/ocean-blue palette, and 8px spacing rhythm
are reused. No global header or navigation section was added. The preview lists
the original, recording count, and dependent shared builds; explicitly acknowledges
no undo and the limits of local cleanup. A separate unchecked confirmation is
required. Opening, closing, refreshing, or revisiting a receipt never posts a
removal. Inspection/AI jobs are not started by the review.

On success, the current Space's capture/timeline/map/world projections are dropped
and reloaded; memory playback unmounts and its object URLs are released. The
confirmation dialog is owned by the Space rather than its gallery card, so a
background refresh cannot destroy confirmation when the card disappears. The
receipt dialog opens automatically. Active submission prevents dialog dismissal
and warns before browser unload; this cannot prevent a browser crash or forced exit.

After an uncertain POST, the client reads current status, without automatically
repeating a destructive request. If impact changed or no removal was confirmed,
the new preview requires another explicit review and checkbox. If both the result
and its status are unknown, only status checking is offered. The directory remains
reachable at Space → Captures → Removal status after the original disappears.

Receipt pages refresh every 10 seconds while open, including completed pages so
access revocation can clear the visible history. Errors clear old receipts and
offer an explicit refresh. Pending totals remain visible on older pages. The UI
explains operator escalation, but cannot force an unconfirmed worker to complete.
Closing the dialog cancels its reads/timers. No receipt or session is persisted in
browser storage. Native removal is deliberately disabled until offline cache,
outbox, and route behavior are qualified. Other browsers' downloaded copies are
not claimed to be recalled.

## Preventing resurrection and false completion

The additive `atlas_build_sources` relation records capture-to-build dependencies.
Existing reconstruction snapshots are backfilled on store opening; newly queued
builds record dependencies in their queue transaction. The relation survives
scrubbing a build's old source metadata, so removal of a second source still
finds that same pending build. Dependencies are removed only after its files are
cleaned up.

Withdrawn builds retain only a minimal failed-job record with an explanatory
message, not source metadata, camera geometry, or surface-review details. Existing
terminal-state checks reject late publication. Queued builds can be cleaned
without launching a worker. Claimed, completed, failed, or older builds require
explicit supervisor acknowledgement that their process group stopped. A stale
heartbeat, a lease timeout, or restarting the relay is not that acknowledgement.
The supervisor records release only after its existing process-tree shutdown
returns successfully. If that cannot be confirmed, cleanup remains pending.

Memory analysis rechecks source/job availability before subsequent weather,
audio, transcription, visual-preview, and AI stages. An already-admitted external
request may finish, but its result cannot repopulate deleted context. Cleanup
waits for the matching analysis completion callback. Old interrupted analyses
without that callback remain pending rather than being presumed dead.

`atlas_source_operations` tracks admitted original uploads, recording uploads,
and metadata inspection. A recording transfer or inspection pins its capture;
an original upload pins its Space until its digest is known. This conservatively
delays completion for already-admitted transfers that might be re-uploading the
same original. Staging filenames include the operation ID. Operations are
released only after their staging files/readers are closed and cleaned up.

Recording decoding is shielded from cancellation of its awaiting HTTP task. A
timeout still waits for that bounded decoding thread to stop before releasing
its operation. If process termination, cancellation, or a cleanup error leaves
the outcome unknown, the durable operation remains and blocks a false completion
claim. There is no deadline-based automatic lease deletion.

A retained per-Space digest prevents an old browser/native outbox from silently
resurrecting the same removed original on retry. This does not remotely delete
the phone's original or prevent an independently authorized copy elsewhere.
Deliberate restoration/re-import policy remains unfinished.

### Android withdrawal observations

The native read cache now retires the affected Space snapshot on an observed
404, as it already did on a 403. This applies to Space detail, original media,
memory context, and generated-world reads. A missing source and a withdrawn
source are not distinguishable by these read endpoints. Retiring the entire
containing snapshot avoids preserving its old capture notes or derived links.
The cache generation rejects detail reads admitted before that invalidation;
other credentials and other Spaces retain their own cached records. A later
successful authorized read may cache a fresh snapshot. Temporary server errors
still do not imply loss of read access, and a failed optional write alone does
not clear an otherwise readable Space.

Removed-original replays return HTTP 409 with the stable `capture_removed` code
and existing user-readable detail. Other Atlas errors retain their existing
response shape unless an explicit code is set. Android's upload worker uses this
specific code, not English message matching, to retire the affected snapshot.
It leaves the queue row in its existing actionable failure state, without an
automatic retry or a false saved badge. Generic 409 conflicts do not imply
source withdrawal. An upload refusal because the Space is missing also clears
that Space's snapshot.

These changes delete only cached remote metadata. The phone's original bytes,
checksums, upload-to-credential bindings, and private drafts are not removed or
retargeted. Cache invalidation and failed upload state survive process restart.
Tests use disposable files and local mock HTTP servers, not a connected phone.
Older relays without the error code still refuse the replay, but cannot provide
this specific invalidation signal; deploy the matching relay/client contract.

This does not enable native removal writes. A native confirmation feature still
needs durable handling of an uncertain POST across app death, status recovery,
and physical-device acceptance. It also does not recall metadata from a phone
that has remained offline and has not observed a withdrawal. Those copies must
remain part of the disclosed offline-copy/retention policy, not a claim of
instant remote erasure.

### Android memory-audio admission

The existing native memory-asset route reached the media interceptor, but that
interceptor's MIME allowlist excluded the audio types that the relay accepts for
recordings. `atlasPlaybackMime` now admits MP3, MP4 audio, WAV, WebM audio, Ogg,
and FLAC only on the existing UUID-scoped memory-asset route. Codec parameters
are separated from the MIME type and legacy WAV/M4A aliases are normalized.
Original photo/video and world-media types retain their existing behavior;
HTML, SVG, script, JSON, and unknown audio MIME types remain refused.

Credential scope is still enforced by `AtlasSession.api` before the media request.
The same 64 MB attachment stream bound, no-store/nosniff responses, redirect
refusal, and stream closing rules remain. The TypeScript native client test
verifies that memory audio takes this binary path rather than a base64 bridge
message or a URL containing a bearer. Playback remains user-selected in the
existing memory UI. No microphone permission, new player library, or automatic
playback was added. This qualifies admission and transport behavior, not codec
decoding or audible playback on a physical target device.

## Cleanup, retained data, and operational limits

`atlas_removals` retains minimal receipts, capture-owner identity, requester
identity, and the digest needed for replay prevention. It does not retain the
deleted story, media, or old annotation snapshots. A receipt temporarily contains
managed recording/build IDs and an analysis ID until their cleanup completes.
There is a 1,000-receipt cap per Space and a 256-unfinished-operation cap per Space.
Receipt expiry and account-erasure handling are still required policy/work.

Cleanup handles at most 20 pending receipts per pass, ordered by last attempt so
an unknown old worker cannot permanently starve later receipts. It is retried by
the relay every 10 seconds, by receipt polling, after withdrawal, and following
supervisor release. Relay shutdown waits for an ongoing cleanup thread before
closing its database. Filesystem or checkpoint failures retain pending receipts.
Space/capture lookups and pending-receipt ordering have dedicated indexes; the
periodic cleanup query need not repeatedly sort the completed receipt archive.
This is not a representative production-load measurement.

Only exact canonical UUID filenames/directories beneath the Atlas store's known
media and reconstruction directories are eligible. Cleanup rejects redirected
parent directories and does not follow a job-directory symlink into unrelated
data. It does not sweep the workspace, generic temporary folders, or other jobs.

Potentially large generated-directory deletion happens outside the Atlas database
lock. Completion rechecks active operations atomically. SQLite secure-delete is
enabled and WAL truncation is attempted without waiting on a busy reader; a
pinned WAL keeps cleanup pending for a later pass. These checks cover the active
SQLite files, not filesystem snapshots or forensic storage erasure.

## Rollout, rollback, and remaining gates

Upgrade/drain relay and reconstruction workers together before enabling removal
for real users. Older workers do not emit release acknowledgements; older relays
do not register transfers or block removed-source re-import. An older binary is
therefore not a qualified rollback after withdrawals. Never remove the new tables
or restore an older database/media backup into service without reconciling current
removal receipts first.

Pending crashed/legacy operations need a deliberate operator recovery workflow
that proves the relevant process/readers stopped and handles their exact staging
files. There is no public force-complete endpoint. This workflow, deployment
acceptance, and fault recovery under actual supervisor crashes remain gates.

Web impact confirmation, local projection invalidation, durable status, and
retry/escalation wording are implemented. Native route admission, offline cache
invalidation, and native receipt UI require their own tests. Removing a contributor after they leave a Space,
account/Space deletion, ownership recovery, export tracking, selective audience
changes, and restoration are separate unfinished parts of LA-04/LA-01.

Backup retention/expiry is not configured or measured here. Production backup
restore must apply newer removal receipts before reopening access. Public worlds
and broader participant AI remain gated until the full consent/retention workflow
is implemented and accepted.

## Verification

- Subsequent native-observation/audio checkpoint: **117 Android fakeDebug unit
  tests**, **217 bridge-core**, **63 bridge-node**, **35 bridge-publish**, and
  **14 bench** tests pass, with no failures/errors/skips in their XML reports.
  This includes cold-process cache eviction, original-byte/checksum retention,
  typed replay refusal versus ordinary conflicts, scoped audio MIME admission,
  and existing capture/import/draft/inset tests. The relay/spatial suite passes
  again with **1,471 tests** after adding the replay error code. Focused console
  native/memory tests pass (**32 cases**); the full console suite then passes
  **1,405 tests in 115 files**, and ESLint passes. The native webview
  build also passes. Robolectric native-access and existing bundle/deprecation
  warnings remain. No physical-device installation, media deletion, provider
  call, or production rollout was performed.
- Web extension checkpoint: full relay/spatial suite **1,471 passed**; after the
  final scope-local pagination and capability edits, **38 removal, collaboration,
  and memory-context cases passed**. Full console suite **1,403 passed in 115 files**;
  focused final removal/Space integration tests, web/native builds, ESLint, Ruff,
  and diff whitespace checks also pass. No Android JVM or physical-device removal
  qualification is claimed. Receipt and confirmation code are lazy loaded; no new
  dependency was added. Existing map/world/main bundle warnings remain.
- Full relay and spatial regression: **1,470 passed**. After adding the final
  lookup/cleanup indexes in the earlier backend-only checkpoint, the **15 removal and owned-worker lifecycle cases**
  passed again. Existing Starlette/AnyIO deprecation warnings remain visible.
- Cases cover signed-author removal, viewer/legacy/outsider refusal, stale
  confirmation, original/recording/context/history cleanup, per-Space replay
  prevention, server-derived attribution, queued and running build invalidation,
  two removed sources in one build, old dependency backfill, and preservation of
  unrelated originals/files.
- Failure cases cover a pinned SQLite reader, filesystem refusal, unknown
  operations across restart, late analysis output, removal during original and
  recording uploads/inspection, and an HTTP timeout while decoding is still alive.
  A generated-directory cleanup test verifies another Space-detail read can finish
  while the filesystem operation is held.
- Owned, disposable child-process tests verify both successful process-group
  shutdown followed by cleanup and a simulated shutdown-confirmation failure that
  stays pending. They do not run a reconstruction engine or operate a device.
- The web extension adds explicit confirmation, unknown-response, conflict,
  receipt pagination, polling/access-refusal, unmount, native-write refusal,
  malformed-response, and unsaved-editor tests. See the latest verification below.
- Changed Python files pass Ruff; `git diff --check` passes. No dependencies or
  new service framework were introduced. Existing bundle-size warnings remain.

No external provider call, user-media deletion, device operation, git push,
deployment, or PR merge was performed. PR #338 remains held. The loopback-only
preview was restarted against its existing store to review the new UI. Desktop
and 390px-wide review opened the impact preview and cancelled it; the original
Austin example remained available, and its removal directory contained zero
receipts. No real AI, membership, media, or device operation was requested.
