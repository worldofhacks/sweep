# Local G-01 diagnostics

This records the preceding stationary session, which was stopped and disconnected.
For the current integrated source and launch path see [unified fleet control](unified-fleet-control.md).

The laptop console stays on `http://127.0.0.1:5173/`. This composition preserves
the map, navigation-review, and gesture interface from `e3f2d05` and adds the
authenticated ground-observation protocol from PR #292. PR #295 supplies the
normalized LiDAR-frame fix; PR #298 supplies the vendor-owned encoder reader.

This is a stationary diagnostics profile. `relay.app:app` authenticates devices,
records observations, and checks actual MediaMTX traffic, but has no command
dispatcher. It refuses intents with `downstream_unavailable`. Platform map and
navigation service composition is not installed in this profile; retaining the
interface does not mean those backend services are available.

The current private configuration admits only wire device ID 11, labelled G-01.
It does not create a simulated roster. Ground robots have two onboard cameras
and one LiDAR; aerial drones have one camera and a depth/proximity sensor whose
infrared specification still needs verification. Only one G-01 camera feed is
commissioned here. Unconnected cameras and unqualified position data must remain
unavailable. Devices, camera paths, and source bindings are explicit so additional
robots or drones can be added without replacing the console.

## Bindings and startup

Private deployment files belong under the ignored `.sweep/ground-runtime/`
directory and must remain mode 0600. Do not commit credentials or field recordings.

- `SWEEP_ADAPTER_BACKEND=remote`, with only the intended device's adapter key.
- `SWEEP_NODE_TYPES_JSON={"11":"ground"}` selects the actual device protocol.
- `SWEEP_DEVICE_UNITS_JSON={"11":1}` declares the operator label without changing
  the authenticated wire ID. Effective units must be unique within a device type.
- `SWEEP_MEDIA_STREAMS_JSON={"11":"ground1"}` binds both media evidence and the
  console's explicit primary-camera playback path. Effective paths must be unique.
- `SWEEP_OBSERVATIONS_FILE` declares the exact session, source, local coordinate
  frames, and permitted connection epochs. No world registration or measured
  source-clock mapping is implied by those declarations.
- The robot uses the same session in `SWEEP_SESSION`, wire ID 11 in
  `SWEEP_DEVICE_UNIT`, `SWEEP_SPOTTER=0`, and `SWEEP_ALLOW_NO_LIDAR=0`.
  `SWEEP_MOTION_ALLOWED` is not a runtime safety gate; do not rely on it.
- Set `SWEEP_RELAY_CLOCK_OFFSET_MS` from a measured transport-clock comparison.
  This does not qualify sensor capture times or localization.

Run the standalone relay from this checkout with its private environment:

```sh
uv run --env-file /absolute/path/to/relay.env uvicorn relay.app:app --host 127.0.0.1 --port 8010
python3 tools/console.py build
python3 tools/console.py start
```

`tools/console.py` owns only the console. Its private runtime file supplies the
relay session and media reader configuration. Stop the previous owned console
before starting this checkout; the launcher never falls back to another port.
The device connection uses explicit ADB reverse mappings for 8010 and 8554.
Do not restart a vendor process or replace an active device runtime implicitly.

The independently owned camera supports `SWEEP_CAMERA_STREAM=ground1` and a
bounded `camera.sh probe`. A successful probe proves advancing producer output;
also verify increasing MediaMTX inbound bytes and decoded browser frames.

## Current physical limits

G-01's retained encoder diagnostic reports `missing_encoder_reply`. The updated
runtime subscribes to the vendor sampler instead of issuing competing `apos`
queries. A fault keeps odometry invalid and drive authority false. LiDAR scan
publication also needs valid odometry and measured mounting calibration.

Stationary telemetry and video do not qualify autonomous driving. Continuous
encoder reception, calibrated fresh LiDAR coverage, a confirmed local stop,
measured motion limits, and supervised drive/stop evidence remain required.
Preserve each robot's original source/configuration and deployment manifest for
rollback; another robot's successful qualification does not qualify G-01.
