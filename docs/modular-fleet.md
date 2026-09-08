# Modular fleet integration

Sweep is one operator console for an additive fleet of aerial drones and ground robots.
Vendor adapters provide only their implemented operations and real measurements. Buttons,
gestures and language share authenticated identities, previews, confirmation, current
readiness checks, command acknowledgement and audit history.

| Class | Current integration scope | Equipment per device |
|---|---|---|
| Ground robot | At least five, including two additional units awaiting provisioning | Two onboard cameras and one LiDAR |
| Aircraft | Existing DJI units and future compatible adapters; no fixed product fleet size | One camera and an owner-reported infrared depth/proximity sensor |

The aircraft sensor model, interface and validated readings remain unverified. It is not
LiDAR and its presence does not prove obstacle clearance. Counts describe scope; they do
not create connected rows, working feeds or qualified capabilities.

## Identity and provisioning

Provision one distinct global positive device ID and credential in
`SWEEP_ADAPTER_KEYS_JSON`. `SWEEP_NODE_TYPES_JSON` binds each ground device to `ground`;
aircraft use `aircraft`. Signed joins must agree with the configured type.
`SWEEP_DEVICE_UNITS_JSON` explicitly maps display labels independently of the wire ID:
wire 11 can remain G-01. Units must be unique within each class. A rejoin increments the
connection epoch and invalidates old command/pose bindings.

The backend permits up to 64 configured credentials, at most 32 ground nodes, a separately
configured physical aircraft limit, and at most 32 IDs in one intent. These are software
bounds, not qualification for that many simultaneous physical devices. Add only actual
provisioned nodes; do not add placeholder live records to fill the dashboard.

For each additional device:

1. Provision the unique identity, exact compatible adapter and private credential.
2. Bind camera streams, sensor sources, frames, timestamps and connection epochs.
3. Verify real telemetry, advancing media traffic and decoded browser frames.
4. Verify local stop/RC intervention, loss of link, stale readings and reconnect behavior.
5. Record that device's calibration and motion acceptance before enabling its controls.

Adding a unit must preserve other devices' identities, media attribution and command
selection. Vendor-specific acceptance evidence remains specific to its hardware and source
revision. Successful checks on G-02 do not qualify G-01.

## Cameras and telemetry

`SWEEP_MEDIA_STREAMS_JSON` retains an explicit legacy primary path, such as wire 11 to
`ground1`. `SWEEP_MEDIA_CAMERAS_JSON` maps each device to up to eight distinct
`{camera_id, label, stream}` records. Streams are safe local MediaMTX names, unique across
the entire effective mapping. An explicit empty camera list stays empty. A robot's second
camera needs its own publisher and permissions; the console never duplicates the first.
Fresh MediaMTX deployments retain the primary `ground1` through `ground4` accounts.
The [media provisioning workflow](../media/README.md#two-cameras-per-ground-robot-and-additional-units)
generates exact additional publish/read permissions from the same explicit maps,
including a robot's second camera and later units, with missing credentials locked.

The media monitor needs successive increasing byte counts before reporting fresh source
traffic. Each camera expires independently. Missing counters, source claims, old epochs,
unchanged counters and process presence cannot establish fresh frames. Browser decoded
playback is a separate check. Disconnected devices cannot keep live camera status.

Ground pose/status/telemetry use authenticated canonical observation sources. Missing,
stale, invalid or unregistered readings remain unavailable; no world position is synthesized
from local odometry. LiDAR power, USB enumeration, motor start, actual scan coverage,
mounting calibration and qualified encoder pose must all be checked on each robot.
The [Ohmni runtime guide](../adapters/ohmni/README.md) lists the measured settings required
for mandatory obstacle avoidance and bounded driving.

## Capabilities and autonomous operation

A camera stream or authenticated connection does not grant drive/flight authority. The
relay's profile, device capabilities, current epoch, source-bound pose and physical readiness
must all permit the operation. Ground controls are bounded forward/yaw pulses, HOLD/ESTOP
and an externally approved return when configured. Aircraft have their separate flight
profile and RC requirements. Unsupported inputs remain visibly unavailable.

[Unified fleet control](unified-fleet-control.md) distinguishes software integration from
physical qualification, and supervised vertical flight from qualified world navigation.
The map authoring/review service does not dispatch routes. No generated room world, map
label or uncalibrated sensor reading is used as motion authorization.
