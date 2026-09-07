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
