# Autonomous demo software

Build and rehearse the six workflows below while camera calibration and physical
flight acceptance are pending. The software must show which capabilities are
configured, preview movement before confirmation, and retain the result of each
operation. Simulation evidence must remain distinguishable from aircraft evidence.

| Workflow | Intended behavior | Software acceptance |
| --- | --- | --- |
| Hallway flight | Follow the floor-tag route from the lobby to the atrium entrance. | Preview and execute the configured corridor route in simulation; stop on stale localization or changed authorization. |
| Visit a tag | Fly to an approved position above a named tag and hold. | Resolve an existing tag, validate the arrival volume, and reject missing or replaced map identities. |
| Object search and survey | Interpret a request, cover an approved grid area, and report sightings. | Ground the request in configured detector classes and search areas; exercise repeated searches, incomplete coverage, and camera failure. |
| Multiple viewpoints | Photograph a subject from several approved positions. | Preview distinct positions, record a fresh image at each completed stop, and retain image provenance and missing-photo failures. |
| Precision return | Return to the recorded starting mark and hover. | Preserve the specific start position and its map/aircraft identity; confirm arrival from fresh localization. Landing remains a separate operation. |
| Formations and patterns | Perform supported formations and routes in the open grid zones. | Show available shapes, enforce the measured volume and separation, and rehearse transitions. Line and column are the initial formation candidates. |

Apply the operator's 7 ft soft ceiling (2.1336 m), 8 ft hard ceiling (2.4384 m),
and 30% minimum launch battery throughout the demo. Local takeoff-relative height
and mapped world elevation are separate quantities. The initial hallway route
uses a validated route height below those ceilings.

Natural-language requests use the existing grounding, trace, and replay machinery.
An unsupported object description must produce an explicit limitation or a request
for a supported target. It must never turn into an invented detection or an
unreviewed movement command. Frame grabs and native camera photographs must identify
their actual source and resolution.

Software completion requires integrated production paths, meaningful automated
checks, and a repeatable simulation/browser rehearsal. A test that only reads a
fixture does not demonstrate the implementation. Keep failures visible when a
camera, route, selected aircraft, or calibration is unavailable.

Field work remains separate: camera calibration, missing corridor clear widths,
hand-carried localization checks, bridge-phone installation, control qualification,
and physical acceptance of each mission. Existing map distances can be reused.
The provisional wall preview cannot establish flight clearance.
