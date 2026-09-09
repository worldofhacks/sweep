# Localizer-only map bundle

`localizer-bundle/` is built by `build_world_bundle.py` from
`deployments/real-navigation/tag-map-53.json`. It exists only to satisfy
`perception.tag_localization.TagLocalizer`'s `validate_bundle()` admission check.
**It is not the navigation deployment's world bundle** -- another agent is
converting the same source map for navigation, and the two outputs should be
reconciled once both exist, since right now they encode the venue in different
schemas for different reasons.

## Why schema v1, not the v2 "world-bundle"

`TagLocalizer` accepts either a schema-v1 bundle (`tools/map_validate.py`) or a
schema-v2 "world-bundle" (`tools/world_bundle.py`). The v2 path requires a real
`ohmni_slam` registration (paired `observed_tags`/`known_tags` evidence documents
with an actual measured residual) and an occupancy grid. This venue has neither:
its tags were surveyed by camera-based joint reprojection
(`crosszone_joint_reprojection`, see `deployments/real-navigation/tag-map-53.json`),
not an Ohmni SLAM run, and no occupancy grid exists yet. Fabricating an
`observed_tags`/`known_tags` pair that matches the map exactly (residual 0) would
misrepresent a verification step that never happened, so this uses schema v1
instead, which only needs a real tag list.

## What is real and what is structural filler

- **Real**: all 51 non-excluded floor tags (ids 0-53 minus the intentionally-absent
  29, minus damaged 35 and 49) with their positions, orientations, and sizes from
  `tag-map-53.json`, re-expressed under an exact rigid coordinate change (see
  below) -- not fitted, not approximated.
- **Structural filler, not consumed by TagLocalizer**: `zones.yaml`'s five required
  zone ids and `room_graph` nodes/edges, and `obstacles.yaml`'s empty list.
  `map_validate.py`'s schema hardcodes a specific room graph (`113`, `mezzanine`,
  `north_hallway`) and zone names (`lobby`, `kitchen`, ...) that belong to a
  different, earlier building deployment in this codebase, not this venue.
  `TagLocalizer.estimate()` never reads either document's content -- it only reads
  `tags.yaml`. These are copied from `tests/fixtures/mapping` verbatim and labeled
  as placeholders in the documents themselves. The navigation deployment's own
  bundle should carry this venue's real zones and room graph; this one should not
  be mistaken for that.

## The coordinate change

`tag-map-53.json`'s frame origin is tag 38's center. The v1 schema requires tag id
**0** to sit at the origin with zero yaw and a +z normal (`map_validate.py`'s
`_validate_bundle`). Tag id 0 is a real tag in this map, just not at its origin, so
`build_world_bundle.py` re-expresses every tag under `T_new = inverse(T_world_tag0)
@ T_world_tag`: an exact rigid rotation+translation, not a fit or approximation.
Under this transform tag 0 lands exactly at the new origin with identity rotation,
and every other tag's position and orientation relative to tag 0 is preserved
exactly. The bundle's `tags.yaml.T_map_scan` records this same transform, and
`manifest.sources[0].rms_m` is `0.0` with an explicit note that this is the exact
residual of a change of basis, not a fitted registration number.

## Reproducing or updating it

Run `build_world_bundle.py`. It calls `tools.map_validate.seal_manifest` (recomputes
all hashes) then `tools.map_validate.validate_bundle` (confirms admission), and
writes `localizer-bundle-accepted-versions.json` with the resulting
`{bundle_version: content_sha256}` pin for `config/webcam_localization.json`.
