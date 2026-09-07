# Modular fleet integration

Sweep uses one operator console and one shared control path for an additive fleet of
aerial drones and ground robots. Vendor adapters supply the operations, telemetry,
cameras, and sensors that each device actually supports. A new vendor or another unit
extends that fleet; it does not require another operator console or a simulated stand-in.

The canonical laptop URL is **http://127.0.0.1:5173/**. Use
`python3 tools/console.py start` from the canonical checkout and follow the
[operating guide](laptop-console.md). The operator runtime uses real relay data only.

## Scope, configuration, and live inventory

These are different facts and must stay distinct in documentation and UI:

| Fact | Meaning |
|---|---|
| Integration scope | Hardware and capabilities the owner intends to support |
| Configured inventory | Explicit device credentials, classes, camera mappings, and deployment settings |
| Connected inventory | Devices authenticated in the current relay session and reporting current state |
| Qualified capability | A supported operation or measurement with recorded acceptance on the actual hardware combination |

The current integration scope is:

| Device class | Additive scope | Equipment on each device |
|---|---|---|
| Ground robot | At least five total, adding at least two to the prior three | Two onboard cameras and one LiDAR |
| Aerial drone | Existing aircraft plus future compatible units; no fixed product fleet size | One onboard camera and one owner-reported infrared depth/proximity sensor |

The ground scope therefore includes at least ten onboard cameras and five LiDAR units.
Those totals are requirements, not live roster rows or evidence of functioning feeds.
The aerial sensor's exact model, interface, coordinate frame, rate, and validated
measurements are unverified. Do not call it LiDAR or infer obstacle clearance from its
presence. Equipment mounted on a device belongs to that device's inventory; it is not
an independent motion target. Standalone cameras or sensor nodes require an explicit
future identity and transport contract before they can be added as a new node class.

Software admission and selection are bounded separately from physical qualification.
The configured identity and selection ceiling is 64 devices in a session. It permits
the larger mixed inventory; it does not qualify 64 simultaneous robots or aircraft.
The original two- and four-aircraft DJI trials, geometry checks, operator staffing,
RF budgets, and measured safety limits remain specific acceptance constraints.

## Delivery and deployment boundary

This change delivers modular fleet source updates, per-camera console support, and
documentation. It does not redeploy the existing live relay or commission additional
hardware. The running console's build metadata identifies which frontend revision is
actually served; source changes alone do not update that copied production build.

Before deploying the updated backend, record and configure measured
`PlanningConfig.drive_speed_m_s` and `drive_rotate_speed_deg_s` in `SWEEP_PLANNING_JSON`,
and `SafetyConfig.ground_max_speed_m_s` in `SWEEP_SAFETY_JSON`, for the actual robots.
These existing ground-support fields are missing from the current live configuration;
they are not new settings introduced by this task. Start a new relay session. Do not carry the existing
session into that deployment or substitute guessed planning speeds. The current live
relay remains a separate deployment until those requirements are met.

The full nodekit/Ohmni custom telemetry and robot peripheral integration remains
paused in separate work. Browser support for optional telemetry or controls does not
mean those backend and device routes are installed or available. The reported aerial
infrared sensor has no integrated, validated readings in this delivery. Additional
robots, second-camera feeds, and sensors become connected or qualified only through
their own explicit provisioning and recorded hardware checks.

## Identity and vendor adapters

The current node classes are `aircraft` and `ground_vehicle`. The legacy wire names
`drone_id`, `drone`, and `drones` remain compatibility names for shared device IDs and
state collections. They do not imply that a ground robot is an aircraft.

Provision a distinct positive device ID and credential for each node. Bind its class
in `SWEEP_DEVICE_CLASSES_JSON`; a signed join can declare `class:aircraft` or
`class:ground_vehicle`, which must agree with configured identity. Older aircraft
nodes may omit the class claim. Never obtain class by guessing from a display label,
camera name, or telemetry state word.

The relay supplies a class-local `unit` used by labels such as `D-01` and `G-01`.
Units derive from configured identity ordering, so adding or reordering configured
IDs requires checking label and legacy media-path mappings. Stable global IDs and
connection epochs remain the control identity. Each rejoin gets a new epoch; roster
changes and prior-epoch acknowledgements cannot silently rebind an earlier command.

An adapter owns vendor-specific I/O, connection health, local watchdog behavior, and
truthful capability reporting. The shared relay, planner, and arbiter retain command
validation, selection, confirmation, and audit ownership. The existing DJI bridge is
one vendor implementation, not the platform definition. A package name or a stub does
not establish that another vendor's hardware is implemented or qualified.

Use these source contracts when integrating:

- [Relay contracts](../relay/contracts.py), [settings](../relay/settings.py), and
  [state](../relay/state.py) for identity, signed membership, telemetry, and epochs.
- [Adapter protocols](../adapters/protocols.py) and [dispatcher](../adapters/dispatch.py)
  for typed operations, acknowledgements, media results, and dispatch checks.
- [Planner](../planner/README.md) and [arbiter](../arbiter/README.md) for class-aware
  motion and safety behavior.
- [Console contract](../console/src/relay/contract.ts) for the browser's accepted
  projections. A frontend field alone does not extend the backend wire contract.

## Capabilities and per-device control

Expose an operation only when the current relay profile, device class, adapter
capabilities, current connection, and operation-specific readiness permit it.
Unsupported, unreported, degraded, and offline are distinct states. Do not replace
them with success values or controls that send an arbitrary vendor command.

Selection addresses exact registered device IDs. Ground and aerial subsets may share
supported horizontal operations, but aircraft-only takeoff, landing, altitude, and
Flight gestures must not be applied to robots. A camera feed does not make its parent
device eligible for motion. Non-motion camera or peripheral controls require their own
implemented, capability-checked route; mounting hardware does not imply that such a
route is available in a particular relay build.

Previews retain exact targets and connection epochs. Confirmation rechecks current
eligibility; changing selection, losing authority, or reconnecting cannot retarget a
preview silently. Gestures and speech use this same path. Session arm grants the
session permission to request eligible work; it is not proof that motors started.

## Telemetry and freshness

Use the accepted current-epoch core telemetry for position, velocity, state, battery,
link, and positioning quality. Retain vendor status, camera capabilities, and any
additional telemetry only through fields supported by the deployed relay schema.
Every reading needs its source, units, timestamp or age, and validity. Missing data is
unreported, never zero or healthy by default. A requested motor/PWM value is a request,
not measured motor health.

The console expires observations using its own clock even when state messages stop.
Disconnected devices and expired readings cannot keep a current “moving,” “hovering,”
“live,” or control-ready badge. Retained data may remain visible as explicitly last
known history. Reconnection requires fresh reports on the new epoch. A browser that
loses its relay link must not substitute local fixtures or cached successful motion.

## Onboard cameras and media

Each ground robot's two onboard cameras need distinct camera IDs, labels, configured
stream names, and per-camera status. Each aircraft's onboard camera needs its own
mapping. Camera inventory and video transport are separate from capture capability:
a playable feed does not prove photo capture, gimbal control, panorama generation, or
media retrieval.

`SWEEP_MEDIA_CAMERAS_JSON` explicitly maps a device ID to at most eight camera records
of `{camera_id, label, stream}`. Stream names must be safe and unique. When the mapping
is omitted, the existing single primary path remains `drone{unit}` or `ground{unit}`;
the console must not duplicate that primary feed to pretend a robot's second camera
works. A second onboard camera remains unconfigured or unreported until its actual
publisher and mapping are provisioned.

Provision media publisher permissions and recording allowlists from the same explicit
camera inventory. Credentials and camera endpoints are configuration, not source-code
examples of connected hardware. `python -m media.configure` generates a private standalone
MediaMTX/Compose bundle from that inventory without starting services; the recording helper
accepts explicit stream allowlists. Follow [media setup](../media/README.md#generate-an-explicit-deployment-bundle); preserve the
existing DJI primary publisher path until that app's configured path is verified.
Each feed reports its own live/offline/unreported state and frame age. A device can
remain connected while one of its cameras is offline.

## Ground LiDAR and the aerial proximity sensor

For every ground robot, commissioning must verify LiDAR power, transport enumeration,
motor start where the model requires it, fresh valid scan returns, coverage, range
units, mounting orientation, calibration, and the local obstacle-avoidance stop path.
Missing, stale, unhealthy, or unusable required clearance data must block ground
motion. This is an integration and acceptance requirement; a displayed scan is not
evidence that a particular robot's avoidance loop has passed it.

The current generic sensor frame is `kind: lidar_scan`: a bounded scan associated with
its parent device and connection epoch, with pose, angle/range metadata, and centimetre
returns. The display feed is not itself a motion authorization. Raw, uncalibrated
returns must remain labelled in the sensor frame and must not be represented as a
calibrated world-map obstacle layer.

The reported aerial infrared depth/proximity sensor needs model and interface
identification before its readings can be integrated. Do not send it as `lidar_scan`
unless the hardware and contract actually justify that type. Freshness, direction,
range limits, uncertainty, and failure behavior must be validated before any aerial
clearance or obstacle-avoidance claim. Ground LiDAR requirements do not create a
LiDAR prerequisite for aircraft.

## Adding and commissioning a device

1. Record the intended device class, vendor/model, firmware, stable ID, and physical
   safety controls. Distinguish planned, configured, connected, and qualified state.
2. Implement or select the vendor adapter; provision a distinct credential and
   truthful capability set. Refuse unsupported operations with typed outcomes.
3. Configure every onboard camera and sensor separately. Verify units, frame,
   calibration, timestamps, stream identity, and safe source/parent associations.
4. Exercise join, readiness, loss, leave, rejoin, stale telemetry, and stale
   acknowledgements using isolated deterministic tests. These tests never populate
   the operator runtime or count as hardware evidence.
5. With the appropriate operator present, record hardware commissioning for the exact
   combination, including sensor loss, video loss, local watchdog/stop behavior, and
   every capability proposed for use. Keep failed and partial evidence.
6. Add the next unit without changing other devices' identities, credentials, camera
   attribution, or active command targets. Recheck shared geometry and capacity
   budgets before simultaneous operation.

Historical session reports, research notes, and model-specific acceptance scenarios
remain evidence for what they actually tested. A documentation update, configured
entry, passing simulator test, or advertised capability never promotes a device into
the connected or qualified inventory.
