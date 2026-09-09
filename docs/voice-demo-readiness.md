# Voice demo readiness

The field console opens on Voice control. Hold the microphone button, speak, and release.
The transcript can be corrected with **Review transcript**. Movement still requires the
normal route preview and confirmation. Recognition and compilation never send movement.

## Demo sequence, ordered by setup effort

| Feature | Say | Remaining setup / scope |
| --- | --- | --- |
| Raw LiDAR | “Show raw lidar” | Focus the ground robot. Its existing `range_scan` observations render immediately; mounting/world calibration is unnecessary for this sensor-frame view. Returns disappear after 1 second without a current scan. |
| Live video | “Show live video” | Focus a device with working media playback. Camera choice stays local to the view. |
| Live detection overlay | “Show object detections” | Install the pinned ONNX model and camera configuration below. No route, search mission, intrinsics, or world localization is needed for these 2D boxes. |
| Named room / tag navigation | “Go to the lobby”; “Navigate to tag 42” | An accepted map plus the appropriate signed aircraft or ground deployment and current qualified positioning. Ground and aircraft use separate selections. |
| Room survey | “Survey the lobby” | Aircraft navigation plus a configured search area/camera source. Shows route, coverage cells and completion in Voice control. Survey measures camera-evidenced coverage; it does not perform object recognition. |
| Object search | “Find a backpack in the lobby” | Survey prerequisites plus search detection model/source/calibration configuration. Shows findings and acknowledgements in Voice control. Ground object-search missions are not implemented. |

Select the intended devices first, including through a qualified spoken selection command.
The voice review is invalidated when the selected devices, their connection epochs, roster,
or accepted destination catalog change. Starting another utterance also retires that
utterance's pending or still-loading preview.

“Show live video,” “show object detections,” and “show raw lidar” are display-only
phrases. They open the focused device's sensor view inside the voice workspace and
cannot stage a model's movement proposal. The Live module also offers an object
detection checkbox in device inspection.

## Navigation and speech configuration

- Backend `OPENAI_API_KEY` enables Whisper transcription; `ANTHROPIC_API_KEY` enables
  semantic interpretation. The approved room names and aliases now reach both audio
  and typed endpoints. Direct motion/selection language additionally uses the existing
  `SWEEP_QUALIFIED_VOICE_INTENTS` qualification allowlist; named mission reviews use
  the signed navigation/search confirmation workflows.
- Save, approve and select the current world-map revision for the active session.
  The destination catalog must load successfully. A drawn or detected tag alone is
  not a navigable destination.
- Aircraft tag destinations can use the signed navigation deployment's tag bindings.
  Ground destinations can use an explicit `tag 42` alias on an approved room/arrival
  zone in the saved map. These identify the approved arrival location, not the physical
  tag surface. Duplicate names/aliases need clarification; unknown tags are not guessed.
- Deploy the measured aircraft navigation configuration, or the signed ground navigation
  configuration and key, against the same map revision. Ground navigation additionally
  requires current world-pose observations, world-to-odometry registration and node
  identity. Arrival behavior is aircraft HOVER or ground STOP.
- Configure `SWEEP_SEARCH_CONFIG` and `SWEEP_SEARCH_DETECTION_CONFIG` for search/survey.
  Semantic requests are limited to configured aircraft search rooms. Default search
  classes are person, backpack, bottle and suitcase. The existing search factory's
  camera-to-world localization seam remains separate from the live 2D overlay; finding
  world positions may be unreported.
- Serve the new console build and restart the relay from this commit. A branch push
  alone does not update a running relay. Search HTTP requests now retain deployment
  prefixes such as `/field-v2/`.

## Standalone live detection configuration

The setup command builds a private configuration from the relay's actual media roster,
copies or downloads the hash-pinned model, and never restarts a running service. Run it
on the relay host with its existing private environment JSON (or omit `--runtime-json`
when the service environment is already loaded). Use a new output directory:

```sh
python -m tools.live_detection_setup --runtime-json /srv/sweep/runtime.json prepare \
  --camera 1:primary:1280x720 \
  --download-model --output /srv/sweep/live-detection
python -m tools.live_detection_setup --runtime-json /srv/sweep/runtime.json check \
  --config /srv/sweep/live-detection/live-detection.json
```

`1:primary:1280x720` matches the aircraft observed during this audit. Replace it for
other devices; repeat `--camera` for each actual camera. Dimensions must match decoded
frames. Use `--model /path/to/yolox_s.onnx` to reuse the pinned model without downloading.
The default RTSP origin is `rtsp://127.0.0.1:8554`; use `--rtsp-origin` when MediaMTX
is elsewhere, including a separate container. Credentials come from the existing
dedicated media-reader settings, never from the relay token.

The check requires two different fresh analyzed frames from every selected camera and
closes its decoders afterward. Exit 0 means camera/model inference passed, exit 1 means
the live check failed, and exit 2 means setup/configuration failed. Its report contains
no stream credentials. A passing check with zero detections does not establish object
recognition accuracy. Enable the generated file using the setting below, then restart
the relay during a suitable operator window and serve the new console build.

Set `SWEEP_LIVE_DETECTION_CONFIG` to a JSON file on the relay host. The model path is
relative to that file and must remain inside its directory. The loader checks its SHA-256.
Use the YOLOX-s ONNX artifact pinned in `perception/yolox_onnx.py`:

```text
https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx
c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063
```

Example only: replace identities, stream name, RTSP address and resolution with the
actual configured camera. `device_id` is the wire ID, not the displayed unit number.
`camera_id` and `stream` must match `SWEEP_MEDIA_CAMERAS_JSON` (or the primary camera
generated by `SWEEP_MEDIA_STREAMS_JSON`). Decoder resolution must match the stream.

```json
{
  "schema_version": 1,
  "sources": [{
    "device_id": 11,
    "camera_id": "front",
    "stream": "ground1-front",
    "stream_url": "rtsp://127.0.0.1:8554/ground1-front",
    "resolution": [1280, 720],
    "model_path": "models/yolox_s.onnx",
    "model_sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
    "target_labels": ["person", "backpack", "bottle", "suitcase"]
  }]
}
```

Omit `target_labels` to show all COCO classes. Default confidence threshold is 0.6.
The authenticated endpoint is
`/api/sessions/{session}/live-detections/{device_id}/{camera_id}/{epoch}`.
Workers start on demand, stop after 30 seconds without viewers, and never execute
commands. At most eight configured cameras are allowed. Images are bounded to 1080p
pixel count and 1 MiB JPEG. A failed source is reported as failed until a new connection
epoch or relay restart.

Detection view is a sampled live stream (500 ms polling), with each image and its
boxes delivered together. It does not draw delayed boxes over unrelated WHEP frames.
Frames older than 1.5 seconds since relay decode are hidden. Decode time is not
camera-capture latency, and this view is not obstacle avoidance.

## Verification and deployment observations

The isolated tests inject transcription/model outputs and synthetic devices; they
exercise the real compiler, accepted catalog, route preview, confirmation, relay,
node runtime, completion and audit export. They do not certify calibration or flight.
Production console builds continue to reject synthetic adapter traffic.

Validation for this change: 1,311 console tests, 464 language/voice/detection tests,
and 16 aircraft/ground mission integration tests passed. The console production
build, Python lint and targeted frontend lint passed. The pinned ONNX model also
loaded and performed real inference on a blank 1280×720 frame (no detections);
recognition accuracy on the actual cameras remains a hardware acceptance check.

On the hosted console during this audit, September 8, 2026 CDT:

- Login, console bootstrap and the relay connection worked. One aircraft was visible.
- A prerecorded audio upload of “Go to the lobby.” returned the exact Whisper
  transcript with zero emissions. The deployed compiler refused `stale_state`.
  The local rehearsal exposed and fixed a missing `single_still` camera capability
  in language grounding which produced that same refusal.
- The destination catalog returned HTTP 409: **No current approved world-map bundle
  is available.** Map approval/selection remains a deployment prerequisite.
- Hardware movement was not triggered on the hosted session during this audit.

Follow-up commissioning, September 8 at approximately 23:05 CDT:

- The hosted roster still had one aircraft, device 1, epoch 4, with the `primary`
  camera on `drone1` reporting live. No ground robot was connected to that session,
  so live ground LiDAR could not be exercised there.
- A real 1280x720 DJI frame from the concurrent WHEP calibration capture passed
  YOLOX inference in approximately 134 ms. That calibration view had no detections.
- The available SSH key was rejected for the saved deployment account. Hosted
  deployment remains pending server access; neither the relay nor camera publisher
  was restarted by this task. The calibration recording continued independently.

Run focused checks from the repository root:

```sh
.venv/bin/python -m pytest language relay/tests/test_voice.py relay/tests/test_voice_plan.py relay/tests/test_language_catalog.py relay/tests/test_live_detection.py -q
.venv/bin/python -m pytest tools/test_loopback_demo_rehearsal.py relay/tests/test_ground_platform_navigation_execution.py -q
pnpm --dir console test
pnpm --dir console build
```
