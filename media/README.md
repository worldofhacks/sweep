# media

Capability area: Platform. Milestone: M3; one selected live feed is also part of the M2.0 checkpoint.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to stream naming or detection-event transport name one change owner and require cross-review.

MediaMTX accepts one authenticated RTSP publisher per path on `drone1` through `drone6`. Each publisher credential is restricted to its exact path, and a second publisher cannot replace an active source. The console reads WebRTC through WHEP and falls back to low-latency HLS. Both read protocols require the console reader credential. MediaMTX records each stream as five-second fragmented MP4 segments under `recordings/<stream>/` and deletes segments after 24 hours. Video runs on the 5 GHz band and control on 2.4 GHz.

Copy `.env.example` to `.env`, use `openssl rand -hex 32` to generate six publisher passwords plus independent read and admin passwords, then start MediaMTX with `just media`. Docker Compose refuses to start when any media password is empty. A relay started from the same environment polls the local MediaMTX API and projects source health into its state events. The admin API and Prometheus metrics listen on loopback-only ports 9997 and 9998. RTSP uses digest authentication. WHEP and HLS stay on the ground-station laptop because their Basic credentials require HTTPS or a trusted tunnel before they can cross an untrusted network.

Run the source-independent acceptance path while MediaMTX is running:

```bash
uv run python -m media.smoke
```

The smoke run publishes a deterministic H.264 test pattern, checks that a publisher cannot cross into another drone path and that reader credentials cannot publish, and decodes one HLS frame. It then loads the real console playback component in Chromium through the runtime configuration endpoint. The browser proves authenticated WHEP offer/answer, ICE, RTP, and a rendered frame; forces WHEP to fail; and proves HLS renders and advances. Anonymous and incorrect reader credentials must be refused. The smoke reads source health through `RelayRuntime` and requires the session state envelopes to report `live`, then `offline`, then `live` after recovery. It validates the finalized recording with `ffprobe` and writes timestamped JSONL evidence to `.sweep/media-smoke.jsonl`. Pass `--assert-retention` while `SWEEP_MEDIA_RECORD_RETENTION` is set to a short isolated-test value to verify deletion. Its latency fields measure source startup to path readiness and the first decoded HLS frame. They are operational startup measurements rather than glass-to-glass video latency.

The production console starts the playback session for the selected live relay source. It performs WHEP offer/answer and ICE negotiation, attaches the received RTP track to its `<video>`, waits for a rendered frame, and tears down the server session when focus changes or the view closes. A failed WHEP negotiation starts authenticated HLS.js playback and waits for its first rendered frame. The dashboard receives the read-only credential from runtime configuration; it does not compile it into the static bundle. The relay polls MediaMTX readiness and inbound-byte progress, publishing the PR #50 shape `{status: live|offline|unreported, last_frame_at}` with no playback URLs or credentials.

For the literal one-camera proof on Linux, replace the test source with the laptop webcam:

```bash
ffmpeg -f v4l2 -framerate 30 -video_size 1280x720 -i /dev/video0 \
  -c:v libx264 -preset ultrafast -tune zerolatency -bf 0 -g 30 \
  -f rtsp -rtsp_transport tcp \
  "rtsp://sweep-publisher-1:${SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_1}@127.0.0.1:8554/drone1"
```

Open the console playback path, verify WHEP first and HLS after a forced WHEP failure, then record glass-to-glass latency with a visible millisecond clock in the camera frame. Store each observation as one JSONL object with integer `source_timestamp_ms` and `rendered_timestamp_ms` fields, then produce the report:

```bash
uv run python media/latency_report.py whep .sweep/whep-samples.jsonl \
  --output .sweep/whep-latency.json
uv run python media/latency_report.py hls .sweep/hls-samples.jsonl \
  --output .sweep/hls-latency.json
```

The WHEP report evaluates its p95 against the confirmed 300 ms target. The HLS report records p50, p95, and maximum latency without a pass or fail threshold. Keep both reports and the recording.

This VPS has no camera, so the laptop-webcam and one-source glass-to-glass exit remain pending. The checked-in `media/evidence/webcam-acceptance.json` records that hold. Initialize a run-specific artifact on the camera laptop with:

```bash
uv run python media/webcam_acceptance.py
```

Replace the pending fields only with observed WHEP and HLS samples, their generated reports, and the validated recording path. DJI publishing remains tracked separately in #51.

Configuration changes require `docker compose restart mediamtx`. The image is distroless, so use `docker compose logs mediamtx` for diagnosis. MediaMTX's official documentation covers [generic webcam publishing](https://mediamtx.org/docs/publish/generic-webcams), [FFmpeg publishing](https://mediamtx.org/docs/publish/ffmpeg), [browser playback](https://mediamtx.org/docs/read/web-browsers), [recording](https://mediamtx.org/docs/features/record), and [authentication](https://mediamtx.org/docs/features/authentication).

DJI codec compatibility, Android publishing, aircraft-to-browser latency, and four-to-six physical sources remain hardware acceptance work.
