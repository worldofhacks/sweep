# Capture memory context — September 9, 2026

An evidence-backed context layer for real Atlas captures. Open **Memory & sounds**
on a capture card, the optional memory step after upload, or **Worlds → Memories**.
The shared panel follows the console's warm ivory/ocean-blue design. Rooms and
generation jobs retain their existing workflow.

## What this checkpoint implements

- Contributor-written descriptions and feelings, a confirmed date with UTC offset,
  confirmed coordinates, and an optional music title/reference link.
- Immutable original audio/video attachments: ambience, narration, or soundtrack.
  User-initiated playback, sharing-rights confirmation, checksums, and deduplication.
  Reference links are limited to HTTPS Spotify, Apple Music, and YouTube URLs;
  the server does not fetch, download, identify, or license commercial music.
- Local EXIF/video metadata inspection. Time without a timezone stays unknown for
  weather purposes. GPS/date are offered for review, never silently copied into
  confirmed notes. Current device location requires a separate explicit action.
- Optional Open-Meteo weather at the confirmed UTC hour and coordinates. Old dates
  use ERA5 reanalysis; recent dates use forecast-model history. These are hourly
  grid estimates, not exact observations at the microphone. Wind is at 10 metres.
- Optional OpenAI speech transcription of the first 60 seconds of one selected
  ambient/narration track or original video, and structured description suggestions
  from one visual frame plus saved context. Soundtracks are excluded. Visual
  observations, creative suggestions, uncertainties, and contributor feelings are
  separate. AI does not infer feelings from people's faces or voices.
- A local digital audio-level measurement (RMS dBFS), **not** calibrated loudness,
  an environmental sound classifier, or an estimate of wind from audio.
- Versioned notes, optimistic conflict handling, progress polling, partial-error
  reporting, interrupted-job recovery by explicit retry, and saved JSON export.
  The dialog guards unsaved notes on close/Escape; analysis continues server-side
  if the view closes after the start request succeeds.

## Provider setup

Install the locked Python environment with `uv sync`. Pillow reads image metadata;
`ffmpeg` and `ffprobe` must be installed on the relay host for video/audio inspection,
recording admission, audio sampling, and AI visual previews. No new browser SDK is
required for memory processing. Memory UI is lazy-loaded.

Server-side configuration only (never `VITE_` values):

| Setting | Behavior |
| --- | --- |
| `SWEEP_MEMORY_WEATHER_MODE` | Disabled by default. Set `noncommercial` only when eligible for the free service, or `commercial` for a licensed customer deployment. |
| `OPEN_METEO_API_KEY` | Required for commercial mode; requests use Open-Meteo customer hosts. |
| `OPENAI_API_KEY` | Enables opt-in transcription and description requests. Billing/provider limits apply. |
| `SWEEP_MEMORY_AI_MODEL` | Optional Responses-compatible vision/structured-output model override; default `gpt-4o-mini-2024-07-18`. |

The local review preview enables noncommercial weather, but has **no OpenAI key**.
AI transport/output handling has stub-based coverage; real transcription/description
quality, billing, retention settings, and provider failures still need an acceptance
run against the deployment's account. `store: false` on Responses is not a claim
of zero provider retention; consult the account's applicable data policy.

References: [Open-Meteo historical weather](https://open-meteo.com/en/docs/historical-weather-api),
[Open-Meteo forecast API and usage terms](https://open-meteo.com/en/docs),
[OpenAI speech-to-text](https://developers.openai.com/api/docs/guides/speech-to-text),
[image inputs](https://developers.openai.com/api/docs/guides/images-vision),
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

## Data and authorization boundaries

`memory_contexts` and `memory_assets` live in the existing Atlas SQLite database.
Recordings live in its separate `memory-media/` directory. Back up both with the
original Atlas database/media. Originals are never rewritten. Memory tracks are
not capture rows, camera poses, coverage evidence, reconstruction inputs, device
commands, or generation-job approvals. Sharing a memory does not create a public
post. JSON exports can contain precise locations and transcripts.

Workspace-owner credentials can edit, attach, inspect, or request paid analysis.
Space invitations can read memory context and recordings for that space, but do
not grant editing or paid-provider authority. This deliberately does not pretend
client-supplied contributor IDs or the optional Clerk header are verified authors.
Account-bound contributor editing requires the separate identity/authorization work.

Routes extend `/api/sessions/{session}/atlas/spaces/{space}/captures/{capture}/memory`:
GET reads, POST saves versioned notes, POST `/inspect` reads local metadata,
POST `/analyze` starts a bounded background operation, POST `/assets` attaches a
recording, and GET `/assets/{asset}/media` reads its original. All use existing
workspace/space authorization, with no public asset URL or client-side provider key.

Limits: 64 MB per attachment, eight per capture, 256 MB of memory recordings per
space; two media uploads and two context operations per relay process. Decoder
inputs are binary media containers, not playlists or remote URLs. Subprocesses,
derivatives, and provider JSON have limits. An interrupted analysis is retryable
after three minutes; there is no automatic paid job replay or durable worker queue.

## Deliberately not claimed as complete

- Environmental sound-event recognition, song recognition, full-length video
  understanding, spatial audio alignment, and interactive immersive-world playback.
- Automatic weather/time/location certainty from a photo or soundtrack.
- Native Android attachment capture/import for memory tracks. Android can use the
  shared context/read/edit/analysis/playback surfaces through narrowly scoped native
  routes; new track attachments currently require the web console. The original
  Android camera/outbox is unchanged. No APK/device acceptance is implied by a
  web-bundle build or JVM test.
- Account-bound participant editing, attachment deletion/replacement controls,
  collaborative edit history, provider billing budgets, and whole-memory sharing
  outside existing space invitations.

## Verification and local review

- Full current console suite: **1,339 tests in 108 files pass**, including six new
  memory-panel tests and the concurrent landing worktree's three tests. All 1,330
  prior console tests still pass. Run with `pnpm test --maxWorkers=2`; an initial
  unrestricted-worker run exhausted test-worker timing on this host.
- **41 Atlas/backend tests pass**, including 14 memory tests: originals and scoped
  access, version conflicts, real WAV decoding, metadata/time uncertainty, opt-in
  providers, soundtrack exclusion, and stale/interrupted job completion handling.
- **11 Android AtlasStorageTest JVM tests pass**, including the new route-scope test.
- Web and Android web-bundle builds, frontend lint, and changed-backend Ruff checks
  pass. Existing large map/3D/main bundle warnings remain. The memory panel is about
  5.6 KB gzip and the library about 1.6 KB gzip, loaded only when opened.
- Browser against real local Atlas routes: saved QA notes/time/location, attached a
  clearly named synthetic test tone, decoded/played it muted, and retrieved real
  Open-Meteo historical weather for sample Austin coordinates. The source capture
  is a UI screenshot, not a scene photo; these notes explicitly identify test data.
  Its original capture time and map location remain unknown, with no new coverage.

Local manual review: `http://127.0.0.1:8177/`, then Worlds → Memories →
Preview · Shoal Creek neighbors. Screenshots are under `output/playwright/` and are
not repository assets. This preview has real Atlas routes, **not** a fleet relay,
media service, or 3D reconstruction worker, so those unrelated unavailable-service
diagnostics are expected. No physical device command or APK installation occurred.

PR #338 remains held. This checkpoint does not merge or push any branch. Separate
landing-page edits in this shared worktree are outside the memory checkpoint.
