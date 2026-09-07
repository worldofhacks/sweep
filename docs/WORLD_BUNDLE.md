# World bundle v2

World bundle v2 records the static survey inputs for one local `world` frame. It stores the saved Ohmni occupancy image as a hashed drawing and ground-routing prior, measured tags, zones, static hazards, and measured corridor height evidence. The validator returns a candidate snapshot with no approval field. An operator-controlled version-to-hash registry supplies the separate approval decision.

The local frame is right-handed with +x toward the elevator, +y toward the street wall, and +z up. It uses a local survey coordinate system. `map_id`, `bundle_version`, the frame name, and the physical datum let a later frame-contract consumer bind the candidate to its `world` declaration. Device-local camera, body, lidar, and SLAM frames remain separate until a registration consumer supplies their transform.

V1 survey bundles remain valid through `tools.map_validate.validate_bundle`. They retain their `building` frame and their existing hashes. Migrate by authoring a new v2 bundle with a new version and its own external registry entry. Do not rewrite a v1 manifest in place.

## Files

Each bundle uses JSON syntax in `.yaml` files.

| File | Contents |
| --- | --- |
| `manifest.yaml` | Bundle and map identity, local world frame, hashed Gray8 PNG Ohmni occupancy image metadata, SLAM registration residual and threshold, three or more non-collinear tie tags, document hashes, and content hash. |
| `tags.yaml` | Tag36h11 poses in `world`, metric size, source, confidence, bounded observation references, and flight-verification state. |
| `zones.yaml` | Geofence, named zones, and corridors with a centerline, width, altitude range, plus hand-measured clearance and flight-height evidence for every segment. |
| `obstacles.yaml` | Static obstacle and no-fly volumes. |

Tags use `measured`, `surveyed`, or `auto_registered` provenance. A verified tag requires an `independent_tape_measurement` that names another tag, carries a hashed evidence file, and is within its stated error bound. Its evidence path cannot duplicate the tag's automatic observation reference. An auto-registered tag therefore requires an independent tape tie before it can become verified.

The validator accepts a 10 MB Gray8 PNG or P5 occupancy image, 1 MB per document, 512 tags, 128 zones and corridors, 512 obstacles or no-fly volumes, 256 polygon vertices, 256 corridor points, and 64 observation references per tag. The occupancy dimensions must match the PNG or PGM header and its decoded Gray8 image. Unknown or malformed data is rejected.

## API

```python
from tools.map_validate import validate_bundle, validate_candidate

candidate = validate_candidate("/srv/world-candidate")
approved = validate_bundle(
    "/srv/world-candidate",
    {"level-1-2026-09": "<content_sha256>"},
)
```

`validate_candidate` accepts v2 only and returns a parsed copy of each validated document through `candidate.document(name)` plus `candidate.occupancy_bytes()` and `candidate.source_bytes(path)`. It does not grant approval. `validate_bundle` accepts v1 and v2 and requires the exact external registry hash for both. Its v2 result provides the same document snapshot API; later geometry and localization consumers can migrate to the `world` fields without reopening mutable paths.

The occupancy image cannot establish aerial clearance. Flight geometry still needs corridor measurements, static hazard review, held-out checkpoint evidence, tag-visibility evidence, and downstream runtime admission.
