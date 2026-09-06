# Four-aircraft console demo

This launcher serves the built Sweep console and four synthetic aircraft through
the production relay, authenticated WebSockets, planner, arbiter, signed command
wire, acknowledgements, and telemetry. It uses a separate loopback listener,
generated credentials, and a unique session. It does not read `.env` or connect to
an existing relay. Synthetic aircraft are kinematic fixtures, not flight evidence.

## Start

From a checkout with dependencies installed (`just setup`):

```bash
just fleet-demo
```

Open **http://127.0.0.1:8766**. A different count/port can be selected with
`just fleet-demo 2 8767`; one through four nodes are supported. Ctrl-C closes the
demo's listener and nodes. The startup message prints the retained audit directory.

The underlying command can request an available port automatically:

```bash
uv run python -m adapters.sim.demo --count 4 --console-dist console/dist
```

Use the `console_url` printed at startup. No credentials need to be copied into
the browser, a URL, or the frontend bundle.

## Operator sequence

1. In **Control → Fleet**, check that D-01 through D-04 are ready.
2. In **Control → Swarm**, arm the session. In **Gesture**, choose **All ready**
   and confirm the selection. Use **Takeoff** and confirm the four targets.
3. In **Control → Swarm**, translate the fleet and check the resulting telemetry.
4. In **Gesture**, select a subset with the target chips and enable tracking.
   The closed-fist row names the aircraft it can hold; open palm reports that
   capture needs exactly one camera-ready aircraft and a valid room identifier.
5. Hold a closed fist until a HOLD preview appears. Check its frozen targets.
   Thumbs-down cancels. For a new preview, release to neutral, hold a closed fist,
   then release and hold thumbs-up to confirm. The same intent ID appears in
   **Control → Requests** and the relay audit.
6. In **Speech**, typed `select all ready aircraft` and `hold` use the local
   fallback on main. Review and confirm each. The basic demo leaves audio
   transcription unavailable; full language integration is described below.
7. Finish with **Land all**, confirm, and check that all four telemetry records
   report landed. The console's network stop remains available throughout.

Gesture tracking requires browser camera permission and loading the MediaPipe
runtime/model. Tracking stops when leaving the Gesture module. Supported gestures
are capture, selected-fleet HOLD, and confirmation/cancellation of gesture-created
previews. A thumbs-up does not approve a speech-created preview.

## Repeatable browser evidence

```bash
just fleet-browser
```

Install the browser once if required: `cd console && pnpm exec playwright install chromium`.
The runner builds the console, starts its own four-node demo on an available port,
and records screenshots, video, `evidence.json`, and relay JSONL under
`output/playwright/fleet-browser-<timestamp>/`. This directory is ignored by Git.

Only the MediaPipe recognition results are scripted. The browser's media stream,
gesture dwell/release policy, preview and confirmation, relay authentication,
command dispatch, node acknowledgements, and telemetry run normally. Checks cover
subset HOLD, cancellation, low confidence, duplicate suppression, invalidation on
membership change, rejoin with a new epoch, typed fallback, and fleet landing.
The evidence is a software integration check, not measured human gesture accuracy.

Authenticated demo-only disconnect/rejoin endpoints support the browser checks.
Rejoin creates a fresh **landed** fixture at home; it does not simulate reconnecting
an aircraft that remained airborne.

## Language integration

The richer language path is supplied by PRs #160 and #161. The separate integration
checkout combines those changes with this demo and contains
`adapters.sim.language_demo`. Its default mode uses the existing Anthropic compiler
and Whisper transcription transports from process configuration. Its explicitly
selected `--synthetic-inputs` mode exercises valid browser audio uploads and the
real grounded compiler using bounded synthetic provider responses.

In that integration checkout:

```bash
uv run python -m adapters.sim.language_demo --count 4 --port 8767 --console-dist console/dist
```

For repeatable software evidence, run the built console's browser script with
`node scripts/fleet-browser-smoke.mjs --language`. This selects synthetic providers
and labels them in the evidence. It does not claim speech recognition accuracy.
The script covers arm, selection, takeoff, translation, HOLD, return home, and
fleet landing through the Speech module's audio upload, compilation, staged
preview, explicit confirmation, and authoritative completion.

Real microphone/webcam rehearsal and physical drone acceptance remain separate
evidence. Record those with the existing session recorder and relay JSONL after
their hardware and input prerequisites are ready.
