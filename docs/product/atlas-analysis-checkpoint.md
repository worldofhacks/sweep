# Memory-analysis lifecycle checkpoint

September 9, 2026 · local implementation · LA-05 remains partial.

## Outcome

Memory tools now distinguish running, taking longer than expected, stop requested,
and confirmed stopped. An explicit **Stop analysis** action targets the displayed
job ID. Repeating it is harmless; an old ID cannot stop a newer job. Existing
completed drafts are not destroyed by a late cancellation request.

The workspace operator, signed-in capture contributor, or signed-in Space owner
can request a stop within their existing scope. Viewers, other contributors,
unrelated accounts, and legacy invitation bearers cannot. This does not grant
participants permission to start paid analysis: that remains operator-only.

The existing ivory/ocean-blue memory tools, secondary button, progress text and
fine-print styles are reused. No new page, global chrome, SDK, or dependency was
added. The browser polls interrupted and cancelling work until the server confirms
completion. Notes and new analysis remain locked while completion is uncertain.
Android's existing scoped route allowlist admits only the same capture's cancel
route, not administrative or force-stop routes.

## What stopping means

This is cooperative cancellation, not provider transport cancellation. Existing
checks before analysis stages reject cancelled jobs. When the worker returns,
its late transcript, metadata result, weather result, and generated suggestion
are discarded. Original media, saved notes, and attached recordings are not
deleted by stopping analysis. A separately confirmed source removal still has
its own withdrawal/cleanup contract.

A stage already admitted or request already sent may finish and incur charges.
This change does not guarantee a refund, remote-data recall, or zero additional
provider work after the click. The UI says so. OpenAI's documentation distinguishes
background cancellation from synchronous requests, which require terminating the
connection; this implementation does neither and does not switch request mode.
See the [official background-mode guide](https://developers.openai.com/api/docs/guides/background).

## Worker acknowledgement and source removal

An elapsed-time threshold previously allowed an interrupted analysis to be
superseded even if its worker was still alive. Interrupted work now remains
non-editable and cannot be superseded until its actual worker callback returns.

Each admitted analysis registers a durable `memory_analysis` operation in the
existing source-operation table, in the same transaction as job creation. The
existing 256-unfinished-operation limit per Space applies. Only the matching
job/Space/capture completion callback removes its operation. Duplicate old
callbacks cannot release a newer worker's operation.

Source withdrawal still revokes access immediately and may unlink local paths;
an already-open reader or provider request can outlive that unlink. A removal
receipt cannot become `local_removed` while its analysis acknowledgement or
tracked source operation remains unfinished. Cancellation does not bypass this
check. Restarting the store or waiting longer does not invent an acknowledgement.

Unknown crashed workers can therefore remain pending. There is deliberately no
force-complete endpoint. An operator recovery protocol that positively establishes
worker termination remains unfinished; the UI must not imply that waiting alone
will resolve every interrupted job.

## Compatibility and rollback

New servers advertise `can_cancel`. Older servers simply do not offer the stop
button in the new client. Deploy server and clients together when enabling this
flow: older clients do not understand the new active states and may present
misleading controls, although new-server mutation checks still reject them.

Drain and reconcile active jobs before rollback. An older server does not know
how to acknowledge `cancelling` jobs or release newly recorded analysis operations;
rolling back blindly can strand cleanup. Do not delete operation rows merely
because they are old. Cancellation itself introduced no new table; the subsequent
request-identity table is described below. Neither slice destructively migrates
existing captures or context.

## Follow-up: replay-safe analysis requests

New servers advertise `analysis_idempotency`. The memory editor saves pending notes
first, then freezes one UUID request ID, revision, provider selections and optional
recording ID before submitting. If the reply is lost, **Recover this request**
resends those exact fields, without saving again or choosing a new ID. No retry is
automatic. A valid receipt is required before the editor treats the request as
confirmed. Invalid or mismatched acknowledgement leaves it recoverable and prevents
editing or a new run in that panel.

This recovery may submit work if the first request never arrived; the UI explicitly
explains that selected providers may charge for that first execution. If the ID is
already recorded, the server returns a receipt without dispatching another worker,
even while both worker slots are occupied. Recovering a completed, failed, cancelled,
or interrupted request never restarts it. A deliberate fresh intent gets a new ID;
the button then reads **Run another analysis**, with a charge reminder.

`memory_analysis_requests` stores at most 200 keyed request records per capture:
Space/capture scope, request ID, normalized-settings hash, original analysis ID, and
an optional refusal reason. It does not duplicate stories, recordings, transcripts
or model output. Records are not expired or evicted while the capture exists: doing
so could turn a late duplicate into new work. The 200-record limit is a storage bound,
not a spending budget, and does not bound legacy keyless requests. Existing matching
records remain recoverable at the limit. Source removal deletes this ledger in the
same transaction as the source context; a later request gets no source access and
cannot resurrect the capture.

The immediate SQLite transaction checks existing intent before revision checks or
worker reservation. Reusing an ID with different settings returns a conflict. A
matching older ID returns its receipt alongside the latest context, explicitly
indicating whether that analysis is still current; no old draft is restored. The
editor adopts the returned revision, notes and coordinates together, rather than
silently pairing new revisions with stale text. Replies from a replaced client or
capture context do not change the newly displayed editor. A valid late receipt
can still retire its exact original connection's recovery reference.

Refused keyed intents (such as stale revisions or an already-active analysis) also
receive a durable receipt, with no analysis ID and a refusal reason. A late copy
cannot start that refused intent after conditions change. HTTP 200 here means the
request outcome was confirmed, **not** that analysis ran; clients must read the
receipt. The editor loads the latest saved memory and asks the user to review before
making a fresh request. Temporary capacity/storage failures instead remain errors;
the same ID can be retried without silently changing settings. A failed database
write rolls back the request record and source operation and releases acquired
worker capacity.

Compatibility: keyless legacy clients retain their prior behavior, without the
new deduplication guarantee. Old servers do not advertise the capability, so the
new browser does not send the extra field or offer protected replay there. Native
JSON transport preserves the same request body within the existing credential and
Space boundary. No native route, provider SDK or external service was added here.
Do not roll back while keyed requests are unresolved: an old server cannot honor
these receipts. Removing or pruning ledger rows is not a safe retry mechanism.

Limits: a committed job whose dispatcher or process died is still interrupted,
not automatically restarted. This is application-level deduplication, not a
guarantee of provider-side exactly-once billing or recovery of unseen provider usage.

New fixture coverage includes concurrent HTTP requests with both slots occupied,
same-key/different-settings conflicts, older receipts after a newer analysis,
durable refusal, restart across terminal and interrupted outcomes, transaction
rollback, bounded request IDs/history, removal cleanup, lost-response UI recovery,
latest-story adoption, replaced-client replies, and native body preservation.
No real provider was called to exercise these paths.

## Follow-up: browser reload recovery

Before sending a protected analysis request, the browser now saves its frozen
request ID, revision, AI/weather selections and optional recording ID in local
storage. Reopening the memory, reloading the page, or opening another tab restores
**Recover this request** without submitting work. After an uncertain request has
settled, **Done for now** lets the person leave and return. The recovery notice
receives focus at the top of the dialog and uses the existing soft-blue callout;
it is not presented as an alarming global error or another page.

The reference contains no story, coordinates, media, transcript, name or credential.
Storage keys hash the exact connection identity and Space/capture scope. Hashing
is namespace separation, not encryption: same-origin code can read local storage.
Each exact credential bucket allows at most 20 pending references, with a strict
shape and 16,384-character read bound. Entries are not silently evicted or expired. This is
not a global bound across historical credentials and is not a spending limit.

Web Locks serialize mutations across tabs. An existing reference wins unchanged;
discovering one while starting a new request does not submit it automatically.
Missing lock support, corrupt data or a failed reservation blocks submission before
HTTP. Corrupt data is not overwritten. Read-only storage retry leaves manual memory
editing available; recovery asks before replacing unsaved text/place/time edits.
The supported environment is an updated browser over HTTPS or localhost.

Only a validated matching receipt retires its request reference. A late receipt
cannot erase a newer intent, including after an editor or connection change.
Failure to remove the local reference keeps explicit recovery available using the
same ID. Same-tab events and cross-tab storage events refresh recovery state; they
never start network analysis. A validated source-withdrawal receipt from submission,
status or the removal directory also attempts to retire that capture's reference.
This cleanup is best-effort: unavailable/corrupt browser storage must not hide a
confirmed server withdrawal or imply that every device/browser copy was erased.

Browser identity includes the exact base URL, session and bearer. Native identity
uses its immutable credential ID and a separate namespace, not the empty JavaScript
bearer. Native transport and builds are checked, but persistence through physical
Android process death has not been qualified. Credential rotation, cleared browser
data, private browsing and storage eviction are not recovery guarantees. Old
credential buckets can remain until explicitly cleared or acknowledged under that
same credential; no automatic cross-credential adoption is attempted.

## Evidence and remaining gates

Tests use disposable captures and mocked providers, never real paid requests.
They cover scoped authority, explicit exact-job requests, duplicate cancellation,
strict bodies, stale IDs, locked edits, store reopen, duplicate old callbacks,
and finished-draft preservation. Actual test background workers are held inside
inspection and mocked AI generation while HTTP cancellation and optional source
withdrawal occur. Cleanup remains pending until those workers really return.
The no-withdrawal cases retain their originals and discard late generated output.

Browser component tests cover interrupted-state messaging and edit locks,
explicit stop submission, cancellation progress, terminal acknowledgement, and
polling shutdown without retrying paid work. Android route tests enforce the
existing Space boundary. These are not physical-phone or provider acceptance.

Validation after the browser-recovery follow-up:

- `pnpm test`: 1,434 passed across 116 files, including 16 recovery-store tests
  and 32 memory-panel tests. Coverage includes bounded storage, credential/native
  isolation, concurrent reservations, late acknowledgements, reload recovery,
  storage failures, dirty-draft confirmation and validated withdrawal cleanup.
- Android `:app:testFakeDebugUnitTest --tests 'org.worldofhacks.sweep.bridge.atlas.*'`:
  71 passed; zero failures, errors or skipped tests in the generated XML reports.
- Web and Android webview builds, TypeScript, targeted ESLint and
  `git diff --check` passed. Existing large-chunk and runtime warnings remain;
  this slice does not establish a bundle-performance budget.
- Prior server validation remains `uv run pytest relay/tests spatial/tests -q`:
  1,495 passed, with two existing Starlette/AnyIO deprecation warnings and targeted
  Ruff passing. Production Python did not change in this browser follow-up;
  the full backend suite was not rerun for it.
- Real-browser QA used disposable synthetic media, the real Atlas routes/ledger,
  and a fake worker on a separate loopback origin. After intentionally losing the
  first response, a full page reload restored recovery without another POST.
  Explicit recovery from a second tab produced **one worker execution, two analysis
  POSTs and one ledger record**. The first tab observed the acknowledgement through
  the browser storage event. Simultaneous reservation contention is unit-tested;
  this browser check was sequential. No provider, real media or fleet/device
  control was used. Temporary test tabs, server and fixture were removed afterward.
- Loopback preview was restarted after checking it had no active analysis or
  source operations. Read-only UI review confirmed the existing memory layout,
  capture, saved story and recording entry. The preview still has one capture,
  zero removal receipts and zero source operations. Cancellation states were
  tested in isolated fixtures, not by starting paid analysis on that example.
  A subsequent restart loaded the request-identity schema. Read-only browser
  review confirmed the fresh-run wording, unchanged assistance layout and honest
  unconfigured-provider states. It left one capture, zero removal receipts, zero
  source operations and zero request-identity records; recovery itself was tested
  with disposable mocked workers, not the saved preview memory.

Still required for LA-05: per-account/Space spending limits and reservations,
physical-native recovery acceptance, measured usage
and costs, transport cancellation where supported, crash recovery, source-linked
chapter drafting, a consented quality-evaluation set, and real-provider acceptance.
This checkpoint does not qualify AI quality, commercial readiness, or the complete
living-atlas goal. PR #338 remains held; nothing here was merged or published.
