# Measurement worksheet import

`tools/measurement_worksheet.py` turns a completed feet-and-inches worksheet into measured world-tag evidence. It accepts JSON syntax, which is also a YAML subset, so the output uses the same parser as the existing map tools. The importer writes `tags.yaml`, directional independent tape records, `evidence/known_tags.json` for `ohmni_world_registration.py`, and a hash-linked copy of the submitted worksheet. A measured floor zone and authored hazards produce current `zones.yaml` and `obstacles.yaml` documents.

Copy [measurement-worksheet-v1.example.yaml](measurement-worksheet-v1.example.yaml), replace the synthetic values, and retain the completed source file. [humanworksheet.md](humanworksheet.md) is the field worksheet for the operator. Measures use `{"feet": 5, "inches": 3.5}`. Add `"sign": -1` for a negative coordinate or offset. Feet are nonnegative whole numbers and inches are between zero and twelve.

The import has a narrow measurement model. It derives the 4 by 3 horizontal grid from the measured top-left black corner, black-square size, and two center spacings. Grid geometry comes directly from these measurements without a fitted scale, rotation, or translation. The near, far, left, right, and diagonal tape checks are evaluated against those derived centers. They are never used to change them. Tag 0 establishes the local world origin, while the required explicit directions align the grid with the current world-bundle frame.

Wall tags require a calibrated center, height above the named floor, and facing normal. Connector lines require a declared direction, parallel-wall offsets, and endpoint tag ties. Missing orientation, incomplete 3D information, nonparallel walls, repeated tag IDs, or collinear registration ties stop the import with a concrete error. Route, corridor, map-image, and flight-approval workflows use their own evidence.

## Run

```sh
uv run python -m tools.measurement_worksheet \
  completed-worksheet.yaml \
  evidence/worksheet-2026-09-07
```

Choose a new output directory. Its `evidence/known_tags.json` is the independent world input for the registration tool. Pair it with an Ohmni-local observed-tags document:

```sh
uv run python -m tools.ohmni_world_registration \
  ohmni-observed-tags.json \
  evidence/worksheet-2026-09-07/evidence/known_tags.json \
  registration-candidate.json
```

The registration result remains unapproved. Assemble a full world bundle only after its occupancy, manifest pins, and approval evidence are available.

## Worksheet-v1 fields

The top-level document has exactly these fields:

| Field | Meaning |
| --- | --- |
| `map` | Map pins, explicit world axes, and operator provenance. |
| `floor` | Floor ID, named site datum, and its elevation. |
| `tag_black_size` | Physical printed black-square size for every tag in this worksheet. |
| `grid` | Three rows of four arbitrary tag IDs, measured corner, axes, and spacings. |
| `moved_tags` | Explicit planar offsets from a grid position. |
| `wall_tags` | Calibrated wall-tag centers, heights, and normals. |
| `independent_tape_checks` | Named tag-pair distances and error bounds. |
| `registration_tie_ids` | At least three noncollinear tag IDs for the known-world registration input. |
| `floor_zone` | Optional measured volume for a zone and geofence. |
| `authored_hazards` | Optional measured table, light, or other obstacle volumes. |
| `connector_lines` | Optional direction, parallel wall offsets, and endpoint ties. |

All XY entries share the site coordinate frame used by `grid.top_left_black_corner_xy`. The importer subtracts tag 0's derived center before writing the local world documents. This keeps the input measurements intact while satisfying the world bundle's `tag_0_center` datum.
