# Two-phone flight gesture demo

This slice connects one Android bridge per Mini 3 / RC-N1 pair and lets the console
target D-01, D-02, or both. It adds a bounded **body-forward / body-backward pulse**;
ordinary distance-based `translate` keeps its existing meaning.

Installing the app, seeing two registry cards, and passing simulator checks are
separate from completing a physical two-aircraft flight.

## Connect the two phones

1. Build the real DJI flavor from the same revision as the relay and console:

   ```sh
   cd adapters/dji_mini3/pilot-app
   env JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
     ./gradlew :app:assembleProbeDebug --console=plain
   adb devices -l
   adb -s PHONE_SERIAL install -r -g app/build/outputs/apk/probe/debug/app-probe-debug.apk
   adb -s PHONE_SERIAL shell am start -n org.worldofhacks.sweep.bridge/.MainActivity
   ```

   Repeat the last two commands for each explicit serial. The probe APK includes
   the DJI SDK and Sweep bridge; the fake APK does not connect to an aircraft.
   DJI registration uses the developer key configured on the build machine for
   `org.worldofhacks.sweep.bridge`.

2. Unlock each phone. In Sweep **Setup**, enter the same session and relay URL,
   but different aircraft numbers and tokens:

   | Phone | Aircraft number | Credential |
   |---|---|---|
   | First phone / first RC / first aircraft | 1 | `SWEEP_ADAPTER_KEYS_JSON` entry `1` |
   | Second phone / second RC / second aircraft | 2 | `SWEEP_ADAPTER_KEYS_JSON` entry `2` |

   Use `ws://MAC_LAN_IP:8000` on the shared Wi-Fi network. The relay must listen
   on that LAN interface. This leaves each phone's USB port free for its RC.
   USB-only setup can instead use `adb -s PHONE_SERIAL reverse tcp:8000 tcp:8000`
   and `ws://127.0.0.1:8000`; that tunnel ends when USB is disconnected.
   Never give both phones the same aircraft number or token.

3. **Save and connect** stores the token encrypted on the phone. Verify DJI
   registration, then connect each phone to its own RC and aircraft. The app
   should report relay authentication, membership, live aircraft/RC connection,
   and fresh telemetry. Complete each phone's home-pose, control-authority, and
   RC-operator readiness inputs according to the actual setup.

4. Start the composed relay using the deployment's private environment file:

   ```sh
   uv run --env-file PATH_TO_PRIVATE_ENV python -m relay.main --host MAC_LAN_IP --port 8000
   ```

   Use `SWEEP_ADAPTER_BACKEND=remote` and distinct configured credentials. The
   standalone `relay.app:app` does not dispatch commands. For the local console,
   load that same environment into Vite so `/relay-bootstrap.json` supplies the
   matching session/token at runtime. Keep the console on loopback.

The session must match on the console and both phones. A relay restart closes
the previous session; choose a new session and update both phones before
reconnecting. Local credentials and session evidence belong under the ignored
`.sweep/` directory or outside the repository.

## Select and command

Begin with D-01 selected in **Control → Fleet** or the **Gesture** target strip.
Once the one-aircraft path is established, add D-02 to select both ready aircraft
and repeat the flight sequence.

In **Gesture**, explicitly choose **Flight (opt in)**. The default
**Capture / HOLD (default)** profile remains available. Changing profiles stops
tracking, cancels the pending preview and starts a new recording. Download the
current recording first if needed. Choose **Enable tracking** to use the camera;
the action buttons also work while tracking is off.

| Flight-profile pose | Drafted action |
|---|---|
| Open palm | **Arm session** — enables commands; does not start aircraft motors |
| Pointing up | **Takeoff selected** |
| Victory / two fingers | **Forward 0.5 seconds** |
| Closed fist | **Backward 0.5 seconds** |
| I love you / thumb, index and little finger | **Land selected** |
| Thumb up | Confirm the displayed gesture preview |
| Thumb down | Cancel the displayed gesture preview |

Every flight gesture drafts a preview for confirmation. Check its action and
aircraft IDs before confirming. A selection, roster, readiness, or connection
change invalidates the old preview. A retry returns to confirmation.

The Flight panel has action buttons with the bold labels above. Button drafts
use **Confirm and send** or **Cancel** in the dock; thumb gestures act only on
gesture-drafted previews. **Hold**, **Land all**, and **Network stop** remain
available. **Land selected** and **Land all** have different targets.

For the requested demonstration, confirm each step and wait for its completion:

1. **Arm session**. This step is disabled once the session is already enabled.
2. **Takeoff selected**; wait for reported hover.
3. **Forward 0.5 seconds**; wait for completion after neutral settling.
4. **Backward 0.5 seconds**; wait for completion after neutral settling.
5. **Land selected**; verify reported landing on every selected aircraft.

With both selected, takeoff, pulse and landing requests freeze D-01 and D-02.
**Arm session** remains session-wide and emits no aircraft command. The current
dispatcher executes the ordered per-aircraft commands, D-01 then D-02, advancing
after each command completes. A shared confirmation does not schedule simultaneous
starts. Wait for both completion results before drafting the next action.

There is no unattended macro that chains these steps. Session enable does not
power on the phones, RCs, or aircraft; those need their normal physical startup.

## What the pulse means

The signed `body_pulse` command contains `forward_mm_s` and `duration_ms`.
The demo requests **+250 mm/s** forward or **−250 mm/s** backward for **500 ms**.
Forward means each aircraft's own nose direction, so differently oriented
aircraft move in different world directions.

The accepted wire bounds are a nonzero integer speed of at most 250 mm/s in
either direction and an integer duration from 100 to 500 ms. The phone requires
the advertised `body_pulse_v1` capability and executes through the normal flight
controller. Its monotonic pulse clock starts with the first non-neutral stick
frame; the first loop tick at or after the deadline changes the output to neutral.
The configured cadence is clamped to 5–25 Hz (200–40 ms per tick), with 10 Hz as
the default. A further 500 ms of neutral settling precedes completion. The normal
RC, watchdog, HOLD, and stop paths remain active.

The relay's current safety envelope adds one worst supported nominal tick
(200 ms) to the requested duration. At 250 mm/s for 500 ms, it reserves a
**0.175 m radius per aircraft** for geofence and spacing checks; both selected
aircraft therefore reserve **0.35 m beyond the configured minimum spacing**.
This covers the nominal tick budget, not scheduler stalls, SDK latency, braking
or position uncertainty. The requested 0.25 m/s × 0.5 s remains a nominal
**0.125 m**, not a measured displacement or hard real-time guarantee.

Existing pose quality/freshness, geofence, spacing, battery, flight-state and
operator checks still apply. A phone's zero indoor position with zero quality
does not establish usable localization. Resolve the reported readiness or
positioning refusal rather than changing telemetry to make the aircraft ready.
The exact aircraft/RC/phone combination still needs the axis and RC-takeover
evidence tracked in #85, followed by one-aircraft #19 and two-aircraft #20.

Voice qualification is separate from this gesture profile. Stored provider keys
alone do not qualify a spoken command, and the existing speech path must not
silently translate “half a second” into a distance step.
