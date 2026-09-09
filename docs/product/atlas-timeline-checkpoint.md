# Living atlas timeline checkpoint — September 9, 2026

LA-02 is implemented locally as an authorized projection of existing captures and
memory context, with an additive date-correction history. It is **partial**, not
full product qualification: physical-device acceptance, real two-account provider
acceptance, locationless Space creation, richer timezone entry, and the deletion
lifecycle remain outstanding. The overall goal stays active; PR #338 is held.

## What a person can do

Open a Space's **Timeline** tab, browse its calendar chapters, select a chapter,
load more memories, and open the existing original-media and “Memory & sounds”
experience. Media remains on-demand according to the existing gallery's size and
video rules; no new autoplay, AI call, or paid processing starts on navigation.

An authorized editor can choose a day, month, year, date range, exact offset-bearing
time, or unknown date. An optional note records why. Earlier corrections remain
available. Conflicting edits return 409 and preserve typed input; a background
chapter change does not unmount an open correction. A refused refresh clears the
private timeline, its map selection, and an open date panel.

The existing map shows capture-time GPS only for the currently displayed timeline
page. Live people, current capture requests, and the browser's current position
are hidden in this view. Captures without GPS stay in the list without receiving
a fabricated marker at the Space's center. The Space boundary remains place
context, not evidence of each capture's location. Declared memory-note locations
are not substituted for measured capture GPS in this first projection.

## Time is a claim with a source

Selection order is an explicit timeline correction, a memory-note date, a submitted
capture timestamp, an offset-bearing metadata timestamp, then a metadata calendar
date whose zone is unknown. With none of those, the date is unknown. Generated
prose, inferred feelings, upload time, and file modification time never supply an
event date. Factual inspection output saved with a completed analysis can be used;
its AI-written summary cannot.

The projection keeps these separate:

- Original `capture.captured_at`, `uploaded_at`, source, and source bytes.
- Selected time precision, value/end, calendar bounds, supplied UTC offset, source,
  and optional correction note.
- Original date evidence and any source-clock warning.
- Correction revision, actor, server change time, previous selected date, and a
  snapshot of source evidence at correction time.

Unknown-zone EXIF becomes a **calendar date with unknown zone**, never a guessed
UTC instant. Exact corrections require an explicit offset: the two occurrences
of `01:30` during a fall DST transition are distinct instants when their offsets
differ. Named IANA zones are not inferred from coordinates or today's device zone.
The current editor uses explicit formatted text, not a natural-language parser.

Chapters follow the source's calendar date; epoch capture timestamps use UTC.
Within a calendar day, exact instants sort newest first. Approximate dates/ranges
sort by their lower calendar bound and never acquire invented midnight timestamps.
Capture IDs break ties deterministically. Unknown dates come last and sort by
upload time only within the explicitly unknown group. A year-only entry has its
own year chapter; a cross-month range has its own range chapter rather than being
misrepresented as a precise month. This is a calendar memory view, not a globally
ordered forensic or robot event clock.

An explicit “unknown” correction overrides other dates **for timeline placement**;
it does not erase original metadata or restrict its audience. Corrections do not
rewrite memory notes, alter queued weather requests, modify AI inputs, or touch
hardware/planner timestamps. Unifying reviewed date claims into enrichment is a
later consent/provenance slice, not an implied side effect of this UI.

## API, authority, and bounds

Under `/api/sessions/{session}/atlas/spaces/{space}`:

| Method and suffix | Contract |
| --- | --- |
| `GET /timeline` | At most 500 source-linked entries, total, and ordering description |
| `GET /captures/{capture}/date` | Ordered correction history for that capture in that Space |
| `POST /captures/{capture}/date` | `{revision, assertion: {precision, value, end, note}}` |

Every route checks existing Space access. Operators may correct dates. A signed
account owner may correct dates in its Space; an account contributor may correct
only captures whose server-written `account_id` matches that account. Viewers and
legacy anonymous-link holders can read but cannot correct dates. Actor and role
are server-derived, with membership rechecked in the write transaction. No new
fleet or paid-job authority is granted.

Date corrections append to `atlas_capture_dates`, keyed by Space/capture/revision,
inside an immediate SQLite transaction with a revision precondition. The existing
500-capture Space limit bounds the projection; the UI initially renders 24 and
extends that prefix on request. No new cursor/event/queue framework was added.
There is a cap of 100 corrections per capture. Responses use `Cache-Control:
no-store`; revocation cannot recall previously downloaded copies.

Back up the existing Atlas database and media together. The new table is additive;
an older application ignores it and therefore displays the older date semantics.
Rollback must not delete correction history. Account rollback constraints in the
[account checkpoint](atlas-account-checkpoint.md) still apply. Source deletion,
history retention, exports, and backup expiry must include this table in LA-04.

Android's HTTP route allowlist now admits only the timeline and capture-date
suffixes under its already-scoped Space. The online timeline is not added to the
offline metadata cache; loss of access is not silently replaced with a cached
private history. Native social sign-in remains a separate unfinished flow.

## Verification

- Full relay suite: **1,408 passed**, including **13 timeline cases** covering late
  uploads, unknown dates/GPS, offset ambiguity, leap years, approximate ranges,
  future/invalid input, stable ties, duplicate-original identity, correction
  history, role isolation, membership removal, original bytes, and metadata versus
  generated-prose selection.
- Full console suite: **1,385 passed in 114 files** before final copy/spacing polish.
  Timeline tests cover paging/map selection, empty and unknown chapters, precision
  labels, conflict preservation, concurrent chapter movement, and refused refresh.
- Final scoped console run after copy/spacing polish: **40 passed in four files**;
  web and native webview rebuilds and ESLint also passed after that polish.
- Native `AtlasStorageTest`: **12 passed** using fakeDebug/Robolectric, including
  the new Space-scoped timeline route check. Java and the SDK were selected from
  the installed Android Studio/SDK paths; no toolchain installation was needed.
- Web and Android webview builds, ESLint, Ruff, and diff checks pass. The timeline
  is lazy-loaded, approximately 3.7 KB gzip plus 0.6 KB CSS. Existing large map,
  world-renderer, and main-bundle warnings remain visible.
- CUA inspection at the existing 1280×720 viewport and a temporary 390×844 viewport
  verified the loaded timeline, original-media card, correction dialog, reachable
  footer actions, and visible basemap. The viewport was restored. The inspected
  example displayed a May 2024 memory-note date separately from its September 2026
  upload date. No example date was changed and no media was uploaded during review.

The loopback-only preview server was restarted against its same stored examples
to load the new backend routes. It does not run a fleet or media worker. No PR was
merged, no provider was configured, and no real-person invitation or public
publication was performed. Browser viewport and JVM tests are not proof of
physical Android capture, playback, or end-to-end social sign-in.
