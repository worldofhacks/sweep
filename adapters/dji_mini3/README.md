# DJI Mini 3 bridge

This directory starts the one-node Mini 3 and RC-N1 pilot app. The Android project pins Mobile SDK 5.18.0 as the candidate build release and contains direct seams for registration, advanced Virtual Stick, local camera rendering, media listing, and the `visual_advisory` overlay. The physical probe records whether that release works with the exact aircraft, RC-N1, phone, and firmware. [DJI Mobile SDK downloads](https://developer.dji.com/mobile-sdk/downloads/)

The bridge accepts only commands that have already completed relay authentication. Its pure Kotlin `command-admission` module rejects a sequence that is not newer than the last seen command and any command beyond the configured local TTL before the DJI SDK call. Product, authenticated relay, and Wi-Fi or Ethernet links must all be connected before Virtual Stick can start. Loss of any active link sends a zero-velocity hold, disables Virtual Stick, and refuses later dispatch until every link recovers and Virtual Stick is enabled again. The same module records the timestamps of actual SDK sends and derives their observed rate. Its Kotlin/JVM tests run without Android, DJI, network, or credentials.

`PilotActivity` renders the primary DJI camera stream on a `TextureView`. The overlay reports primary-camera coverage, the SDK-reported frame resolution and rate, and readiness as `no_surface`, `no_camera`, `waiting_for_frame`, `live`, or `stale`. The DJI product callback and Android LAN callback feed the watchdog directly. Relay transports report their authenticated connection state through `PilotActivity.onRelayConnectionChanged` and must pass `false` on close, failure, or heartbeat timeout.

`hardware-profile.example.json` is the handoff record for the actual aircraft, controller, phone, firmware, and MSDK observation. It intentionally has blank serial, firmware, phone, and date fields. Fill a local copy during physical bring-up and attach the measured report; the checked-in file is not evidence that a Mini 3 node has passed.

## Bench replay

The Python replay harness has no Android, DJI, network, or credential dependency. It reports recorded bridge observations; command admission remains authoritative in the Kotlin module:

```bash
uv run python -m adapters.dji_mini3.bench \
  --input adapters/dji_mini3/bench.example.jsonl
```

Each JSONL line has one of these shapes:

```json
{"type":"command","sent_at_ms":1000,"round_trip_ms":50}
{"type":"command_rejection","reason":"expired"}
{"type":"command_drop","count":1}
{"type":"telemetry","observed_at_ms":100}
{"type":"video","captured_at_ms":100,"controller_at_ms":220,"decoded_at_ms":245,"delivered_at_ms":280}
{"type":"video_drop","count":1}
{"type":"phone","thermal_c":39.5,"throttled":false,"battery_draw_ma":1200}
```

The resulting report computes the observed Virtual Stick send rate from `command.sent_at_ms` records. It also includes command RTT, RTT jitter, command drops and bridge-reported rejections, telemetry rate, video drops, the three video latency segments plus glass-to-glass p95, and phone thermal, throttling, and battery-draw observations. A guarded 15-minute physical run must record actual Virtual Stick rate, camera capability results, photo/panorama and media-download results, disconnection/watchdog behavior, and RC pause, takeover, RTH, and landing observations.

## Android bring-up

Set `DJI_APP_KEY` in `~/.gradle/gradle.properties`, never in this repository. DJI requires SDK initialization to finish before `registerApp()`, and registration may need the internet on a first install. The app should then be connected through the RC-N1 to the exact hardware profile before any Virtual Stick command is enabled. DJI documents 5 to 25 Hz as the recommended Virtual Stick send range; the bridge records its actual accepted-send cadence for the bench report. The Mini 3’s exact telemetry, camera, panorama, media, video, and watchdog support remains a physical probe. [DJI SDK manager](https://developer.dji.com/api-reference-v5/android-api/Components/SDKManager/DJISDKManager.html) · [DJI camera stream sample](https://github.com/dji-sdk/Mobile-SDK-Android-V5/blob/dev-sdk-main/SampleCode-V5/android-sdk-v5-sample/src/main/java/dji/sampleV5/aircraft/pages/LiveFragment.kt) · [DJI Virtual Stick](https://developer.dji.com/doc/mobile-sdk-tutorial/en/tutorials/virtual-stick.html) · [DJI V5 sample](https://github.com/dji-sdk/Mobile-SDK-Android-V5)
