# One operator console

Use **http://127.0.0.1:5173/** for the single local operator console. Start the
verified production build with `python3 tools/console.py start` from the canonical
checkout. The launcher owns one loopback port and never picks an alternate.
`python3 tools/console.py status` identifies the exact running build and session.
See [laptop console operations](laptop-console.md) and
[modular fleet integration](modular-fleet.md).

The active deployment must pair its relay URL, session ID, and credential with
the same session used by the real nodes. Check the non-secret URL/session fields
in `/relay-bootstrap.json` against the live relay before declaring the console
connected. Do not publish or paste that endpoint's credential.

Changing code or proving a camera stream separately does not connect its device
to the displayed roster. Verify real identities and moving video frames inside
the actual All devices view. Source availability, browser playback, node
membership, and motion readiness are separate facts.

When replacing the active build, retain the canonical URL, retire previous UI
servers, and update the operator's existing preview. Ports 5174, 5175 and 5181
remain closed, including obsolete redirect servers. Coordinate
the active relay/session with the hardware owner before changing it. A UI upgrade
does not require restarting robots or the relay.

## Motion readiness

`Control not granted` means the node has withheld motion authority. Console Arm
does not clear a local safety interlock. Inspect the deployed node's actual
diagnostics: a disabled drive, missing or stale sensor, nearby obstacle, or local
override may cause the refusal. Resolve the reported condition and let the node
publish fresh signed readiness. Do not synthesize authority or pose evidence in
the console to make controls selectable.

For the verified ground-robot hardware gaps, scan faults and physical checks, see
[Ohmni LiDAR commissioning](ohmni-lidar-commissioning.md).

## Real data and additive devices

The console starts with an empty roster until its real relay reports authenticated
devices. Planned inventory, configured credentials and a hardware profile are not
live membership. Disconnected/stale devices cannot retain a live motion or video
claim; retained readings are historical evidence. Missing data stays unreported.
Synthetic adapters and generated camera publishers are excluded from normal
operation; explicit isolated test fixtures are not operator data sources.

Ground robots in scope have two onboard cameras and one LiDAR each. Aerial drones
have one camera and one reported infrared depth/proximity sensor; its exact
interface remains unverified. Multiple cameras belong to their device identity
and connection epoch, and each stream has independent freshness. Only advertised,
qualified controls are enabled. Drone sensor data is never labeled LiDAR by
assuming a sensor type from the presence of a camera.
