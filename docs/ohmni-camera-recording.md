# Ohmni head-camera recording

Record the existing 640×480 head RTSP feed on a laptop, then extract Gray8 frames
for calibration or tag detection. Each recording contains the raw pixels, a frame
index, hashes, camera identity, and host decode receipt times. Exposure timestamps
and video latency require separate measurement.

Run from the repository root with the locked Python environment. Set
`OHMNI_HEAD_RTSP_URL` to the authenticated URL for the selected ground camera.
The tool reads that variable without storing the URL in the recording.

```sh
uv run python -m tools.ohmni_head_capture \
  --output ./g01-head-run-01 \
  --run-id level1-run-01 --device-id 7 --camera-id g01-head \
  --duration-s 60
```

Use the relay's actual numeric device identity and the measured camera identity.
The output directory must be new. Recording stops at the duration or byte limit;
the defaults allow 60 seconds and at most 512 MiB. The decoder keeps its latest
frame, so the recording may skip frames when the consumer falls behind.

The capture layout matches the local fisheye probe's `ohmni-camera-capture/v1`
format: `frames.gray`, `frames.jsonl`, and `manifest.json`. A successful capture
has a complete manifest and no `INCOMPLETE` marker. Failures retain the marker so
partial output cannot be mistaken for a completed recording.

The manifest records wall time, CPU used by the capture process, and CPU used by all
children reaped during the capture. In the standalone command, that child is the
decoder. An embedding process can reap unrelated children, so its child value is not
decoder-exclusive. Platforms without `RUSAGE_CHILDREN` mark child CPU unavailable.

## Offline tag smoke report

`tools.ohmni_camera_smoke` reads a completed capture through the hash-checking reader and writes a new evidence directory containing canonical observation submissions and a report. It never opens a camera or contacts the relay.

```sh
uv run python -m tools.ohmni_camera_smoke \
  --capture-dir ./g01-head-run-01 \
  --calibration ./g01-head-calibration.json \
  --calibration-sha256 <pinned-calibration-sha256> \
  --output ./g01-head-smoke-01 \
  --session level1-run-01 --device-id 7 --connection-epoch 1 \
  --source-id ohmni-head --camera-serial g01-head --camera-frame camera \
  --tag-size 7:0.1524
```

The capture manifest device and camera identities must match the command. The calibration serial and image dimensions must match the same camera. Each `--tag-size` binds an AprilTag 36h11 ID to a physical edge length in metres. The command accepts up to 64 distinct tag sizes. Synthetic calibration artifacts require `--allow-synthetic-calibration`.

`observations.jsonl` contains one `camera_frame` submission for every selected source frame, followed by any decoded `tag_observation` submissions. Each submission keeps the recorded callback receipt timestamp under a clock ID derived from the recording hash. `t_capture` is `null` because the RTSP decoder does not provide exposure time. The tool never writes relay-owned `t_ingest`.

A detected tag keeps its decoded ID even when no configured physical size exists or IPPE cannot admit a pose. A pose-admitted tag reports `T_camera_tag` as a local camera-to-tag frame relation. Its covariance remains `null` until the PnP uncertainty is calibrated. Camera evidence uses confidence `0`; pose admission does not provide flight authorization. The report records `flight_approved: false`.

The output directory must be new. The tool writes to a temporary sibling directory, fsyncs both files, and publishes the directory after processing completes. It bounds selected frames, output bytes, and tag-size declarations. `report.json` includes raw-frame, index, and calibration hashes; decoded IDs and detector reasons; pose-admission counts; CPU and processing FPS; inherited capture FPS and CPU measurements; and an explicit unavailable latency result.
