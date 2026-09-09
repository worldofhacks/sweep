# Shared-memory contribution checkpoint

September 9, 2026 · local implementation · LA-03 remains partial.

## Outcome

A signed-in contributor can add a story, feeling, declared place/time, music
reference, and permitted audio/video recordings to their own capture. A Space
owner can help edit captures in that Space. Both can explicitly keep a memory for
revisiting. Other contributors and viewers retain playback and reading, without
editing someone else's capture. Original media and capture metadata are unchanged.

The existing memory editor and upload flow are reused. The warm ivory/ocean-blue
palette, subtle depth, and existing spacing remain in place. No new page, global
header, provider SDK, real-time editing service, or parallel annotation store was
introduced. The hero-only landing and fleet controls were not changed here.

The editor states the audience and that earlier versions remain in edit history.
Details & sources now includes on-demand attributed history, with earlier/latest
pages. Account IDs identify editors; they are not represented as verified names.
Legacy reviews without an actor are explicitly labeled as unrecorded, not assigned
to a new account. Downloading current context does not export originals or history,
and its disclosure now says so.

## Authority and compatibility

| Actor | Read/playback/history | Edit notes, inspect locally, attach, keep | AI/weather analysis |
| --- | --- | --- | --- |
| Workspace operator | Existing workspace scope | Existing scope | Existing explicit flow |
| Signed account Space owner | Its Space | Its Space | No |
| Signed contributor | Joined Space | Only captures with its server-written account ID | No |
| Viewer or other contributor | Joined Space | No | No |
| Legacy bearer invitation | Existing invited Space | No | No |
| Revoked/unrelated account | No | No | No |

`can_edit` and `can_analyze` are now separate response fields. Earlier servers
without the latter retain their existing operator-only behavior in the client.
No social account gains fleet, reconstruction, or paid-job authority. Granting
participant enrichment requires the later consent/job-lifecycle slice, not merely
turning on these controls.

Memory notes, recording commits, local inspection commits, and review commits
recheck membership/ownership inside the existing immediate SQLite transaction.
A membership removal during a recording transfer or local inspection prevents
the eventual write. Upload staging and failed final files are cleaned up. A
stale notes revision returns 409; the browser retains typed words and does not
approve stale text. Reloading unsaved work still requires explicit confirmation.

Saving continues to invalidate prior analysis/review, including an explicit save
of unchanged text, preserving existing job-revision semantics. Repeating the same
recording retains existing byte/role deduplication. Repeating the same actor's
review of the same revision/draft does not create another history record.

Timeline date correction and memory editing share one capture-edit authority
predicate. Date corrections remain a separate explicit assertion; this change
does not silently send corrected timeline dates to weather or AI providers.

## Storage, limits, and rollback

An additive `memory_edits(space, capture, sequence, data)` table records saved
notes with previous values, added-recording metadata, and explicit reviews. Actor
and timestamps are server-derived. `memory_contexts` stores only the current
context plus a small last-edit marker; normal memory reads do not load history.

Under the existing capture `/memory` route:

- `GET /history`: latest 20 recorded edits.
- `GET /history/{before}`: at most 20 edits with a smaller sequence, newest first.
  The integer must be in 1–201. No credentials or cursors in query strings.

Responses use the existing Space access checks and `Cache-Control: no-store`.
Each capture has a 200-edit history cap. Hitting the cap refuses the whole change,
including rolling back a recording's database insertion and final file. Existing
8-recording/capture, 64 MB/file, and 256 MB/Space recording limits still apply.
Paging bounds response and rendering size; this is not a high-volume load result.

Back up the existing database and media together. An older application ignores
the new table/fields and restores operator-only memory edits, but does not know
to append history. Any rollback/redeployment gap must be disclosed; this is not a
complete historical audit of older installations. Do not delete the new table as
a rollback step.

LA-04 must include this history, recording attribution, existing transcripts,
derived outputs, and backups in deletion/retention. Editing away a sentence does
not erase its old versions. Recipients' prior downloads cannot be recalled. No
production deletion or privacy-compliance claim follows from this checkpoint.

Android's existing scoped request allowlist admits only the new history suffixes
inside its selected Space. History is not added to the offline cache. Native
social sign-in and native memory-sidecar uploads are still unfinished; this does
not reclassify them as supported.

## Verification and remaining acceptance

- Full relay regression: **1,415 passed**. The account test's old operator-only
  memory expectation was updated to assert contributor editing plus denial of
  analysis; fleet/reconstruction denial assertions remain intact. Two existing
  Starlette/AnyIO deprecation warnings remain visible.
- **Seven collaboration API tests** pass, including two signed test accounts
  adding distinct PNG originals and stories, reviewing, and revisiting their shared
  historical chapter. Cases cover owner assistance, unrelated contributor/viewer
  refusal, denied analysis, stale revisions, duplicate recordings/reviews,
  attributable paged history, original-byte integrity, persistence, legacy
  authorship protection, spoofed fields, and rollback at the edit cap. The final
  focused run also verifies existing recording/history access after revocation.
- Full console regression: **1,389 passed in 114 files**. After final wording and
  spacing polish, the memory/voice-note/native subset passed **34 tests**. Added
  cases cover account editing without analysis controls, preserved conflict
  drafts, on-demand history paging, and refused history-page cleanup.
- Android fakeDebug/Robolectric `AtlasStorageTest`: **13 passed**, including
  allowed history pages, other-Space denial, and unsupported suffix denial.
  This compiles the native route change but does not prove physical-device use.
- Web and native webview builds, ESLint, changed-module Ruff, and diff checks pass.
  The existing lazy memory-dialog chunk is about **10.6 KB gzip**; no dependency
  was added. Existing main/map/world chunk-size warnings were not suppressed.
- Actual preview inspection at 1280×720 and 390×844 checked the memory dialog,
  audience/history disclosure, source details, empty history, and reachable
  footer actions. An inline history-heading spacing issue was fixed and checked
  at phone width. The viewport was restored and the existing example was not
  edited, reviewed again, or uploaded. Real-account populated-history behavior is
  covered by local tests, not a live-provider browser session.

The loopback preview was restarted against the same database for the new routes.
It intentionally has no fleet/WebSocket runtime; it is not evidence for live
device, speech-command, or camera-stream acceptance.

This local work does not complete LA-03. Real Clerk/provider sign-in, two actual
participants on target devices, interrupted native contribution/playback, friendly
member profile names, source deletion/recovery, and qualitative usability remain
unverified. No real-person invitation, provider setup, paid analysis, publication,
git push, deployment, or merge was performed. PR #338 remains held.

Subsequent work adds [source removal and web review/status](atlas-removal-checkpoint.md),
including derived-build invalidation, tracked cleanup, impact confirmation, and
a durable receipt directory. Native removal, operator recovery, and backup
policy are still pending; this does not
turn the broader deletion/consent lifecycle into a completed acceptance gate.
