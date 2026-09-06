# Optional camera recording

This is the source-independent recording slice of M3.1. It does not by itself accept
WHEP playback, readiness projection, latency, DJI compatibility, or multi-aircraft
reliability.

Use `media/recording.py` instead of starting the recording Compose override directly.
The helper supports macOS and Linux with Python 3.12, Docker Compose, `ffmpeg`, and
`ffprobe` installed. It exits with a clear unsupported-host error on Windows.

Before a run, mount a durable, access-controlled evidence destination outside this
checkout. For example, use `/Volumes/SweepEvidence` on macOS or
`/mnt/sweep-evidence` on Linux. That destination is the retained copy: back it up
according to the rehearsal evidence policy and do not rely on the ignored
`recordings/` working directory.

Run the helper with a new bounded identifier and the exact relay session ID:

```sh
python3 media/recording.py \
  --run-id 2026-09-06-run-01 \
  --session-id relay-session-01 \
  --export-root /Volumes/SweepEvidence
```

The command refuses an existing working or exported run and reserves a fresh
`recordings/session-<sha256>/<run-id>/` bind mount. The safe storage key is derived
from the exact relay session ID; the manifest retains that ID unchanged. The helper
also refuses to adopt, replace, or stop a pre-existing MediaMTX service for the same
Compose identity. A host lock prevents another helper from controlling that project
or container, even when it uses a different recording root.

The helper starts only MediaMTX and remains in the foreground. Follow logs from
another terminal with `docker compose logs -f mediamtx`. Press Ctrl-C once the
publishers have stopped or the run is complete. `SIGHUP` and `SIGTERM` also take the
orderly stop path and remain caught until validation and durable publication finish.
The first catchable signal requests that orderly path. If pre-publication
finalization must be abandoned, send a second catchable signal; the helper aborts,
restores its handlers, and leaves the stopped working run unexported for inspection.
The helper stops its exact owned MediaMTX container before reading its output,
fully decodes the selected video stream in every finalized MP4 under explicit
duration, dimension, stream-count, per-process, and one-hour aggregate finalization
bounds through the publication commit, hashes every segment, and atomically publishes
a canonical `recording-manifest.json` to
`<export-root>/<run-id>/`. Zero-segment runs fail and remain unexported. The working
run and durable root directory identities are pinned and revalidated throughout;
replacement or unmount fails closed instead of redirecting monitoring or evidence.

The default run budget is 20 GiB, four hours, and 10 GiB of free space reserved for
the relay, audit log, and operating system. Both the recording and durable
filesystems must pass preflight. The configured duration must be positive and cannot
exceed 23 hours, leaving a hard margin below MediaMTX's 24-hour working retention.
While recording, the helper checks elapsed monotonic time, bytes, and free space
every 500 ms, and the interval cannot be configured above one second. It checks
storage again after MediaMTX stops, after segment validation, and immediately before
publication. Reserve calculations include the canonical manifest, destination
allocation blocks, archive directories, and publication metadata rather than only
segment logical bytes. Crossing a recording limit stops MediaMTX, preserves valid
evidence when the durable destination still has its reserve, records `safety_limit`,
and exits nonzero; it is never reported as a successful operator stop. If a
cross-filesystem export would breach the durable reserve, finalized evidence remains
in the fresh working run for manual recovery.
An unexpected MediaMTX exit is archived as `service_failure`, never mislabeled as a
storage limit or signal stop, including when an exit and a duration, storage, or
operator boundary race. If aggregate pre-publication finalization expires, no archive is published and the stopped working run
remains available for explicit recovery; use shorter evidence runs on a host that
cannot validate the planned media inside that bound. Override a recording budget
only from an evidence plan that accounts for
source count, bitrate, run duration, and the required host reserve:

```sh
python3 media/recording.py \
  --run-id 2026-09-06-run-02 \
  --session-id relay-session-02 \
  --export-root /mnt/sweep-evidence \
  --max-bytes 10737418240 \
  --min-free-bytes 10737418240 \
  --max-duration-seconds 7200
```

MediaMTX also deletes active working segments after 24 hours as a last-resort age
bound. The helper's earlier duration deadline prevents a valid run from reaching
that deletion window. A successful export moves or verified-copies the fresh run out
of the working directory, so later runs cannot include earlier media. After an
uncatchable `SIGKILL`, host power loss, or runtime crash while the Docker daemon
remains up, immediately stop the container ID belonging to that run with
`docker stop --time 20 <container-id>`; the helper prints that immutable ID in its
`recording_started` event. Do not reuse that run ID or restart recording into its
directory; preserve the entire hashed-session/run directory for manual recovery and
start a new run ID.

The Compose image is pinned to the MediaMTX 1.20.1 multi-platform manifest digest.
Each recording manifest binds that image, the relay session, and SHA-256 identities
for the MediaMTX and Compose configuration; the helper refuses a configuration that
changes while the container starts. fMP4 timing describes ground-station
arrival and container presentation timestamps only; it does not establish camera
capture time, phone clock alignment, frame correspondence, or another clock
transform.

Keep the phone ZIP and its hash with the durable run. The rehearsal manifest should
also bind phone/device identity, camera mode, stream path, and the map, calibration,
and clock identities. Recording does not change publisher, reader, playback, or
control-API authorization.
