# Ohmni measurement worksheet

Write every field before transcribing the measurements into `measurement-worksheet-v1.example.yaml`. Keep the original completed file with the survey notes. The importer copies its exact input into the evidence directory as `worksheet-source.yaml`.

## Map and floor

- Map ID and map version:
- Survey date and operator:
- Floor ID:
- Site datum name:
- Floor elevation from that site datum: ___ ft ___ in
- World +X direction: toward elevator
- World +Y direction: toward street wall
- World +Z direction: up

The map frame uses tag 0's center as its local datum. Record the site coordinate of the grid's top-left black corner so the worksheet preserves the original measurement frame.

## 4 by 3 floor-tag grid

- Printed black-square size: ___ ft ___ in
- Top-left black corner, X: ___ ft ___ in
- Top-left black corner, Y: ___ ft ___ in
- Across direction: +X
- Down direction: +Y
- Center spacing across: ___ ft ___ in
- Center spacing down: ___ ft ___ in

| Row | Left | Second | Third | Right |
| --- | --- | --- | --- | --- |
| Near | | | | |
| Middle | | | | |
| Far | | | | |

Record independent tape measurements after placing the grid. Use the IDs in the table.

- Near edge pair and distance:
- Far edge pair and distance:
- Left edge pair and distance:
- Right edge pair and distance:
- Diagonal pair or pairs and distance:
- Maximum permitted error for each measurement:

A moved floor tag needs its ID, then the measured `dx` and `dy` from its grid location. The values are along the declared world axes.

## Wall tags

For each wall tag, record its ID, center X and Y in the same site frame as the grid corner, center height above this floor, and the normal it faces (`+x`, `-x`, `+y`, or `-y`). A wall tag without a normal or height has no usable 3D pose.

## Floor area, hazards, and connector lines

A floor-zone polygon needs all corners, plus lower and upper heights. A table, light stand, or other authored hazard needs its footprint and lower and upper heights. The importer writes zone and obstacle documents only when those dimensions are present.

A connector line needs its direction axis, two wall offsets whose walls are parallel to that direction, and two tag IDs that tie its start and end to the map. The importer records it as worksheet metadata. Navigation corridors and clearance claims need their own survey.
