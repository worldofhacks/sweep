# Offline map survey tools

The tag extractor converts four surveyed 3D corners per tag into a full pose. The
bundle validator checks file integrity, geometry, floors and an explicit version
allowlist. Both run on a laptop or this development server. Their output supports
map authoring; flight acceptance still needs the measured site and integration
checks below.

## Run the synthetic example

From the repository root, with the existing Python environment:

```bash
cp -r tests/fixtures/mapping /tmp/sweep-map-example
.venv/bin/python -m tools.tag_pose_extract \
  /tmp/sweep-map-example/survey.json /tmp/sweep-map-example/tags.yaml \
  --preview /tmp/sweep-map-example/preview.html
.venv/bin/python -m tools.map_validate /tmp/sweep-map-example \
  --accepted-version synthetic-three-tags-v1
```

The extractor reports three tags. The validator reports `valid: true` and
`flight_approved: false`. Open `preview.html` in a browser to inspect the plan and
elevation projections: red is printed right, green is printed up, blue is the
printed face normal. The axes are 0.3 m long. This preview shows tag axes only;
cloud, route and formation-volume visualization belongs to the grid work.

The three-tag fixture is synthetic. Its tiny PLY contains three tag centers and
provides no obstacle or free-space evidence. Its overlapping example zones are
unapproved placeholders. `expected_tags.json` stores independently specified
centers and axes that tests compare against actual extractor results.

## Collect a real survey

1. Scan the installed tags and surrounding site with the phone. Export the
   textured mesh or point cloud for CloudCompare on a computer.
2. Register each detail scan against the spine scan. Save its proper 4×4 transform
   and RMS residual in meters. Preserve the original scan files for hashing.
3. Use CloudCompare's point-picking tool to select the four **black-square outer
   corners** on the textured mesh. A corner click records an XYZ coordinate in the
   scan, rather than a pixel coordinate in a photograph. Copy those four XYZ rows
   from the exported picked-points file into `survey.json`, following the fixture.
   This first tool accepts normalized JSON; it does not import arbitrary
   CloudCompare export headers or perform ICP registration.
4. Confirm the tag's decoded ID and canonical printed orientation against its
   source image. Enter corners in TL, TR, BR, BL order as seen from the printed
   front. Enter `orientation_confirmed: true` only after this check. Measure the
   black-square side in meters for `size`. Add its `floor_id` and an approximate
   outward `front_normal_scan` in scan coordinates.
5. Supply `T_map_scan` and run the extractor. Review the tag-axis preview and
   compare five tag-pair distances per area with tape measurements, within 0.03 m.
   Re-pick discrepant corners. Preserve the measured distances with the survey.

A square's winding determines a normal but cannot identify its printed top or ID.
The operator confirmation is required evidence of that check. The supplied front
normal must agree with the computed normal within 15 degrees. Each corner must
lie within the smaller of 1 cm or 5% of the measured side from the fitted square;
wrong size, crossed corners and excessive nonplanarity are rejected. These are
input-quality limits, not measured localization accuracy.

## Frame and pose contract

All distances use meters and all angles use radians. The building frame is
right-handed: +x toward the elevator, +y toward the street wall, +z up. Tag 0's
center is the origin. The operator must register scans to these directions;
the extractor checks rigidity and the origin but cannot verify physical building
alignment from corner coordinates alone.

Tag 0 retains its surveyed orientation, recorded as `frame.tag0_yaw_rad` in the
manifest. This resolves the conflicting request for building-aligned axes and
Tag 0 yaw zero: yaw is zero only when the installed tag actually aligns that way.
No hidden yaw normalization rotates the building.

`T_map_scan` maps scan points to building points using column vectors:
`p_map = R_map_scan * p_scan + t_map_scan`. Only proper rigid transforms are
accepted: no reflection, scale or shear. If the scanner exported another unit,
convert the scan and all picks to meters before registration.

Tag-local +x is printed right, +y printed up, +z out of the printed front.
`T_map_tag` maps tag-local points to building points with the same convention.
The center is `x,y,z`; `normal` equals its third rotation column; `yaw` is the
azimuth of its first rotation column. A vertical printed-right axis is rejected
because this yaw is undefined. The complete rotation remains authoritative for
floor, wall and tilted tags. In local coordinates the corner order is:

```text
TL = (-size/2, +size/2, 0)    TR = (+size/2, +size/2, 0)
BL = (-size/2, -size/2, 0)    BR = (+size/2, -size/2, 0)
```

OpenCV optical axes use +x right, +y down, +z forward. A downstream localizer must
perform that explicit conversion and invert a map-to-camera PnP transform to
recover the camera pose. A camera pose becomes a drone/body pose only with tested
gimbal and extrinsic transforms. These tools do not change the arbiter or camera
calibration contracts.

## Bundle files and hashes

The `.yaml` documents use JSON syntax, a subset of YAML. The tools deliberately
accept that subset only. Duplicate object keys and nonfinite numbers are rejected.
The fixture provides complete editable examples of each schema:

| File | Required content |
|---|---|
| `manifest.yaml` | Schema version 1, bundle version, UTC creation timestamp, metric units, building frame, floor IDs, source file hashes and registration transforms/RMS, document hashes and content hash. |
| `tags.yaml` | Extractor output, including unique tag36h11 IDs (0–586), graph floor, measured size, full transform, center, yaw, normal and orientation confirmation. |
| `zones.yaml` | A closed XY geofence with height bounds, named zone polygons with floor/height bounds and approval flags, and a room graph with floor-associated nodes. |
| `obstacles.yaml` | Obstacle IDs, graph floor references, conservative closed bounding polygons and height ranges. |

Tag IDs share one namespace throughout the building. Graph nodes distinguish
Level 1 from the mezzanine. The 113 west-to-mezzanine and east-to-north-hallway
edges are separate and non-autonomous. Required named zones are `lobby`, `kitchen`,
`atrium`, `launch` and `corridor`. Polygons must be closed, simple and nonzero-area;
height bounds must increase. The geofence check covers surveyed tag centers and
allows boundary points. It is a map-domain boundary; flight-volume insets and
obstacle clearance belong to derived geometry and the arbiter.

`files` hashes the exact bytes of tags, zones and obstacles with SHA-256. Every
source path is relative to the bundle and its exact bytes must match its hash.
`content_sha256` hashes the manifest with that field removed, serialized as JSON
with sorted keys, compact separators, ASCII escaping and no nonfinite numbers.
The accepted bundle version must be supplied externally with `--accepted-version`;
reading a version from the bundle itself would defeat the stale-version check.

After an intentional edit, assign a new bundle version and regenerate hashes:

```bash
.venv/bin/python - <<'PY'
from tools.map_validate import seal_manifest
seal_manifest('/tmp/sweep-map-example')
PY
```

Then validate with the intended version allowlist. Sealing only records current
content; it does not validate geometry, approve a polygon or grant permission to
fly. Hashes detect mismatches, but do not authenticate the author or prove that
measured geometry is correct.

## Remaining acceptance

Real acceptance still requires saved scan registration evidence, tape-distance
checks, glass and obstacle inventory, owner approval of the annotated lobby,
kitchen and atrium polygons, and held-out checkpoints within 0.10 m. The map
validator allows unapproved polygons for offline authoring and always reports
`flight_approved: false`.

The relay still needs runtime map loading/version pinning and the wrong-map
hold/refuse-arm drill. Grids need conservative unknown-space handling, the actual
route tube, geofence insets, formation bounds and a cloud preview. Localization
needs calibrated delivered video, ambiguity rejection, delayed-measurement replay,
body/gimbal extrinsics and tested hold/land integration. A nearby tag alone does
not establish visibility. Synthetic tests cannot close any of those physical gates.
