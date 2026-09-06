# One operator console

Use **http://127.0.0.1:5173/** for the local operator console. Vite uses port 5173
with `strictPort`, so starting a duplicate process fails instead of silently
creating another console on a different port.

The active deployment must pair its relay URL, session ID, and credential with
the same session used by the real nodes. Check the non-secret URL/session fields
in `/relay-bootstrap.json` against the live relay before declaring the console
connected. Do not publish or paste that endpoint's credential.

Changing code or proving a camera stream separately does not connect its device
to the displayed roster. Verify real identities and moving video frames inside
the actual All devices view. Source availability, browser playback, node
membership, and motion readiness are separate facts.

When replacing the active build, retain the canonical URL, retire previous UI
servers, and update the operator's existing preview. An old bookmark may redirect
to the canonical console; it must not continue serving another build. Coordinate
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
