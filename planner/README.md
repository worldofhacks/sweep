# planner

Capability area: Autonomy. Milestone: M1.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to safety-relevant planner paths name one change owner and require cross-review.

Deterministic and unit-tested. Formations (line, column, circle, grid, V) around a center with spacing; translate; altitude; sweep lanes (lawnmower per drone, lanes assigned by current position); come home with staggered pads and a second call to land; hold; select; `capture_room`; and `map_area`. Known-map area capture resolves the room graph and approved capture poses, assigns rooms, plans collision-checked routes, and schedules capture tasks. Allocation is nearest-drone-to-target. Everything is clamped to the mode's box before it becomes a command.

PRD: sections 5.3 and 5.4 (modes: indoor constrained is the capstone mode).
