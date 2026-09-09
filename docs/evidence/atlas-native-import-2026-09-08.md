# Android existing-media contribution — 2026-09-08

This advances the full Atlas objective; it is not full product or physical-device
acceptance. The preceding goal turn made verified progress on shared application
design and map behavior. This turn addresses the missing Android library path.

## Implemented

- Shared, Spaces-styled source selector: native camera/360 capture or Android
  document import. Import selects up to ten JPEG/PNG/WebP/MP4/WebM files without
  requesting broad library, camera or location permission.
- Original workspace/space/contributor binding is captured before opening the
  picker and retained across activity recreation. Selected-source read grants
  persist while needed and are released when no import still depends on them.
- WorkManager copies documents to private files before upload. Native SQLite holds
  the durable state; resume starts a fresh copy rather than appending a partial one.
  Completed bytes are synced and checksummed before the existing upload worker
  can send them. Process death at the copy-to-upload handoff is recoverable.
- Per-file limit 64 MiB; active copies reserve space inside the 1 GiB device quota.
  File labels remain readable in the queue; source URIs never enter its web-facing
  summary. Display names cannot choose local paths, and exports use the media's
  actual allowed suffix. Existing version-1 outboxes migrate without rewriting files.
- Copying, upload waiting, failure, and server-checksum-confirmed saved states are
  distinct. Retry/export/removal do not label a partial copy a completed original.
- New web and Android imports retain `captured_at=null` and `position=null`.
  Modification dates and current GPS are not capture evidence. Camera submissions
  still require capture-time timestamps. Original embedded metadata stays unchanged.

## Verified

- Console: **89 files / 1,231 tests passed**, including five new Android UI operation
  tests. The final UI copy/file-label edits passed those five tests again. Full
  ESLint and TypeScript builds pass; desktop and bundled Android assets build.
- Relay/spatial: **1,193 tests passed**. The new HTTP test uploads real PNG bytes
  through a scoped invitation, restarts the store, and retrieves the identical
  original with unknown capture time and zero inferred GPS coverage. Camera
  submissions with missing/null capture time are refused.
- Android: **78 fake / 98 probe unit tests passed**, including **14 import tests**
  per variant. These exercise ContentResolver streams, actual private files,
  SQLite migration/reopening, exact native HTTP upload, temporary/missing-source
  failures, empty/oversized files, quota reservation, shared-grant cleanup, MIME
  and filename handling, and scheduling after the committed-copy handoff.
- Both APKs assemble and both lint variants pass. **26 existing warnings per
  variant** remain; three new KTX warnings were fixed without suppressions.
- Built Android UI: **nine final layout observations** pass at 390×844, 320×568
  and 768×1024 across the source chooser and upload queue. Both source choices
  fit in the initial dialog view, are keyboard reachable, and keep the document
  within the viewport. Upload-list scrolling leaves bottom navigation visible.
  The explicit import choice sends exactly one simulated `importMedia` operation
  with the original space/session binding and no camera operation. The 320px
  chooser and queue screenshots were visually inspected.
- The user preview was restarted with the updated relay contract. A read-only
  artifact verifier confirms the existing 11-source, 98,951-triangle model remains
  ready, with unchanged SHA-256
  `e1ea6aa6c8f16a47e5ad83f9eaafa6e14b4cc6b632c26d5e879bfe1669b9f9b7`.

## Evidence boundaries

Native UI screenshots use the **actual built Android assets and shipped CSP**,
but native IPC and upload-state examples are simulated; real local Atlas reads
populate the space. They do not prove an Android picker opened or a handset sent
files. JVM ContentResolver providers and local HTTP servers likewise do not prove
physical Android scheduling, permission prompts or CameraX/sensor behavior.

ADB was checked during this turn and reported no connected Android devices. No
camera/microphone was opened, no device commands were sent, and no emulator
license was accepted. Physical provider/permission/reboot tests, georeferenced
production-qualified reconstruction, fuller missing-surface guidance, notification
delivery and other requirements in the product ledger remain outstanding.

Deploy the relay update before the clients. An older relay rejects null import
timestamps; the native outbox retains the original for retry after the update.
HEIC/MOV import and EXIF-derived capture-time/location interpretation are not
implemented; unsupported formats are refused, not quietly converted.
