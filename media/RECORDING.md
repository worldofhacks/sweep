# Optional camera recording

This is the source-independent recording slice of M3.1. It does not by itself accept
WHEP playback, readiness projection, latency, DJI compatibility, or multi-aircraft
reliability.

Use `media/recording.py` instead of starting the recording Compose override directly.
The helper supports macOS and Linux with Python 3.12, Docker Compose, and `ffprobe`
from FFmpeg installed. It exits with a clear unsupported-host error on Windows.

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

The command refuses an existing working or exported run, reserves a fresh
`recordings/<session-id>/<run-id>/` bind mount, starts only MediaMTX, and remains in the
foreground. Follow logs from another terminal with `docker compose logs -f
mediamtx`. Press Ctrl-C once the publishers have stopped or the run is complete.
The helper stops MediaMTX gracefully before reading its output, validates every
finalized MP4 with `ffprobe`, hashes every segment, and atomically publishes a
canonical `recording-manifest.json` to
`<export-root>/<run-id>/`. Zero-segment runs fail and remain unexported.

The default run budget is 20 GiB with 10 GiB of free space reserved for the relay,
audit log, and operating system. Both the recording and durable filesystems must
pass that preflight. While recording, the helper checks bytes and free space every
500 ms; crossing either limit stops MediaMTX, exports any valid finalized segments,
and exits nonzero with `safety_limit` in the manifest. An unexpected MediaMTX exit
is archived as `service_failure`, never mislabeled as a storage limit. Override a
budget only from an evidence plan that accounts for
source count, bitrate, run duration, and the required host reserve:

```sh
python3 media/recording.py \
  --run-id 2026-09-06-run-02 \
  --session-id relay-session-02 \
  --export-root /mnt/sweep-evidence \
  --max-bytes 10737418240 \
  --min-free-bytes 10737418240
```

MediaMTX also deletes active working segments after 24 hours as a last-resort age
bound. A successful export moves or verified-copies the fresh run out of the working
directory, so later runs cannot include earlier media. If the helper is killed with
SIGKILL while the Docker daemon remains up, immediately stop `sweep-mediamtx` with
`docker stop --time 20 sweep-mediamtx`. Do not reuse that run ID or restart recording
into its directory; preserve the entire session/run directory for manual recovery
and start a new run ID.

The Compose image is pinned to the MediaMTX 1.20.1 multi-platform manifest digest.
Each recording manifest binds that image, the relay session, and SHA-256 identities
for the MediaMTX and Compose configuration. fMP4 timing describes ground-station
arrival and container presentation timestamps only; it does not establish camera
capture time, phone clock alignment, frame correspondence, or another clock
transform.

Keep the phone ZIP and its hash with the durable run. The rehearsal manifest should
also bind phone/device identity, camera mode, stream path, and the map, calibration,
and clock identities. Recording does not change publisher, reader, playback, or
control-API authorization.
