# Mapped formation previews

`planner.mapped_formations` implements the software-planning foundation for live
issues #87 and #88. It supports line and column for two aircraft and line, column,
wedge, and diamond for four aircraft. The planner searches every feasible slot
assignment, minimizes clearance-checked route cost with deterministic tie-breaking,
and moves aircraft in stable identity order.

Formation authorization is separate from destination-navigation permission. Every
request requires a formation volume carrying the exact accepted map and geometry pins,
an explicit zone allowlist, owner approval, an enabled flag, a per-zone speed cap, and
an explicit altitude offset for every slot. Four-aircraft plans require distinct
altitude offsets. Full horizontal and vertical aircraft, uncertainty, tracking, and
stopping envelopes must fit the volume, accepted grid, and every slot-to-slot gap.
Approach swept volumes may not overlap.

No zone name is special-cased. In particular, navigation permission does not grant
formation permission and the planner does not infer a kitchen fallback. The current
live issue scope calls for separately measured and owner-approved lobby and
atrium-front volumes.

Every result remains `dispatch_eligible=false`. This module creates no command, relay
message, adapter call, live capability claim, or takeoff. Simulation, accepted map and
localization evidence, relay/arbiter/adapter integration, staffed RC safety operators,
and recorded physical acceptance remain separate gates before flight use.
