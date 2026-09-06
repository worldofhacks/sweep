# POC mission runtime

Run `uv run python -m relay.main` to compose navigation, mapped formations, search,
language, and the configured adapters. The console requests a route preview and
confirms the same frozen plan before dispatch. A changed map, selection, connection
epoch, or expired preview requires a new preview.

The deployment uses these configuration files and identities:

| Setting | Purpose |
| --- | --- |
| `SWEEP_NAVIGATION_CONFIG` | Pins the map, geometry, motion limits, zones, arrival slots, and deployment evidence. |
| `SWEEP_CONTROL_LOCALIZATION_CONFIG` | Pins each aircraft's measured sources, capture-clock mapping, and freshness limits. Required for remote mapped motion. |
| `SWEEP_MISSION_CONFIG` | Enables configured formations and search areas, with camera geometry and permissions. |
| `SWEEP_ENABLE_LOCALIZED_NAVIGATION` | Explicitly enables approved remote route control when set to `true`. |
| `SWEEP_PERCEPTION_KEY` | Authenticates the signed detection publisher; use a credential distinct from the relay and aircraft keys. |
| `SWEEP_LOCALIZATION_KEYS_JSON` | Gives each localization publisher its own aircraft credential. |
| `SWEEP_DETECTION_CAMERA_IDS_JSON` | Binds each detection producer to its physical camera identity. |
| `ANTHROPIC_API_KEY` | Enables grounded text compilation. |
| `OPENAI_API_KEY` | Enables recorded-audio transcription before grounded compilation. |

See [navigation deployment](NAVIGATION_DEPLOYMENT.md),
[mission configuration](mission-runtime-config.md), and the
[localization protocol](CONTROL_LOCALIZATION_PROTOCOL.md) for the configuration
contracts. Configuration changes take effect when the relay restarts.

Start the localization publisher with
`uv run python -m perception.control_publisher --config publisher.json` and feed
measured sensor JSONL on standard input. A live publisher uses its calibrated
monotonic clock and emits loss states while input stalls. The
[publisher guide](CONTROL_LOCALIZATION_PUBLISHER.md) describes replay mode, source
records, and clock binding. The phone must import the matching localization pins
and bounds while disconnected and landed before connecting to the relay.

Start the detection producer with
`uv run python -m perception.detection_publisher --config detector.json` for the
active mission identity. Set its `mission_id` to the search preview allocation's
`task_id` with the final `:<drone_id>` removed; that value binds the mission version
and epoch. Search counts coverage only after a processed frame
passes capture timing, localization, camera identity, camera height, and mission
checks. Arriving at the end of a route without eligible frames leaves search
incomplete. A sighting must belong to an accepted processed frame and match the
mission's target class.

The RTSP webcam path supplies observation-only timing. Its decode receipt time
does not establish camera capture time, so it cannot certify coverage or control
localization. A physical deployment must supply measured source timing, camera
calibration, body-camera extrinsics, floor elevation, map and tag survey pins,
and the required flight probe evidence. Synthetic maps and replay records support
software checks without establishing those physical facts.

The integration tests exercise the composed app's preview, confirmation, simulated
route execution, search completion, localization loss, and stop preemption:

```sh
uv run pytest relay/tests/test_mission_integration.py \
  relay/tests/test_control_localization_integration.py \
  relay/tests/test_route_ownership.py
```
