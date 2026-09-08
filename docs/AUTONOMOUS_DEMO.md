# Autonomous demo software

The demo software has simulated routes for hallway flight, named-tag visits, marked
precision return, and line and column formations. Each route uses the production
navigation runtime, approved map pins, command-by-command revalidation, and
simulated arrival evidence. The tests cover stale localization, a replaced map
identity, a changed marked-aircraft epoch, and aircraft that lose required
separation. The fixture uses a simulation approval and no hardware adapter
configuration.

| Workflow | Intended behavior | Software acceptance |
| --- | --- | --- |
| Hallway flight | Follow the floor-tag route from the lobby to the atrium entrance. | Preview and execute the configured corridor route in simulation; stop on stale localization or changed authorization. |
| Visit a tag | Fly to an approved position above a named tag and hold. | Resolve an existing tag, validate the arrival volume, and reject missing or replaced map identities. |
| Object search and survey | Interpret a request, cover an approved grid area, and report sightings. | Ground the request in configured detector classes and search areas; exercise repeated searches, incomplete coverage, and camera failure. |
| Multiple viewpoints | Photograph a subject from several approved positions. | Preview distinct positions, record a fresh image at each completed stop, and retain image provenance and missing-photo failures. |
| Precision return | Return to the recorded starting mark and hover. | Preserve the specific start position and its map and aircraft identity; confirm arrival from fresh localization. Landing remains a separate operation. |
| Formations and patterns | Perform supported formations and routes in the open grid zones. | Show available shapes, enforce the measured volume and separation, and rehearse transitions. Line and column are the initial formation candidates. |

## Current route evidence

`planner/test_demo_workflows.py` executes each of the four navigation workflows
through `NavigationRuntime` with simulated aircraft state:

- The hallway route rejects a pose older than its configured freshness window.
- A named-tag route binds its configured arrival slot and refuses after the map
  pin changes.
- Precision return reaches the dedicated marked slot, then refuses when the
  aircraft connection epoch changes.
- Line and column plans execute their sequential routes. Each refuses a live
  state where two aircraft overlap their motion envelopes.

Visual search reports detector classes, confidence, and image coordinates. A map
position remains unavailable until a configured provider supplies fresh calibrated
camera attitude and transform evidence. Control poses establish aircraft position;
they do not establish camera orientation.

Software completion requires integrated production paths, meaningful automated
checks, and a repeatable simulation/browser rehearsal. A test that only reads a
fixture does not demonstrate the implementation. Keep failures visible when a
camera, route, selected aircraft, or calibration is unavailable.

Run the route rehearsal with:

```sh
uv run pytest -q planner/test_demo_workflows.py
```

The wider navigation tests cover deployment parsing, tagged-slot measurement,
flight-wire admission, signed arrival retention, and formation revalidation.
The Android admission test runs with the local SDK when a phone bundle is built.

## Configuration boundary

A deployable hallway, tag, return, or formation route needs a signed navigation
approval, current map and geometry pins, an approved destination zone, retained
localization, and the selected aircraft epochs. Named-tag bindings also require a
tag in the pinned map and one measured approach slot inside the configured
horizontal and height bounds. Precision return binds one aircraft epoch to one
marked slot. Formation bindings define a separately approved volume, layout, and
speed limit for line or column.

The route checks revalidate these inputs before every segment and every completed
arrival. A changed pin, approval, roster, pose, or separation state produces a
refusal. Arrival means hover. Landing is a separate command.

## Field prerequisites

Physical rehearsal requires calibrated cameras, measured corridor clear widths,
hand-carried localization checks, installed bridge phones, qualified control
operators, and a physical acceptance run for each mission. The initial route must
stay below the 7 ft soft ceiling (2.1336 m) and 8 ft hard ceiling (2.4384 m), with
at least 30% launch battery. Local takeoff-relative height and mapped world
elevation are separate measurements.

The map distances may guide setup. The provisional wall preview does not provide
flight-clearance evidence.
