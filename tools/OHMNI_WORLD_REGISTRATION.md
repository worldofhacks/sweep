# Ohmni world registration

`tools/ohmni_world_registration.py` fits an Ohmni SLAM frame into the world frame from matched tag centers. It creates an unapproved candidate. A tape check or another approval workflow must decide whether the candidate becomes usable.

The solver uses a proper 2D rigid transform only: translation in meters and yaw in radians. It never estimates scale or a reflection. Three or more matched fit tags are required. Fit tags must be noncollinear and have a planar condition number at most 100.

Every fit-tag residual must be at most 0.100 m and RMS residual must be at most 0.075 m. Held-out tags do not participate in fitting; each must be within 0.100 m. The command refuses the candidate when any bound fails, including a single outlier.

## Input

Provide two separate JSON documents. Their `provenance.sha256` values must differ. The observed document holds local SLAM coordinates; the known document holds independently measured world coordinates. Coordinates are in meters.

```json
{
  "schema_version": 1,
  "frame": "ohmni_slam",
  "provenance": {
    "name": "ohmni-scan-2026-09-07",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "tags": [
    {"tag_id": 4, "xy_m": [1.2, -0.4]}
  ]
}
```

The known document has the same shape with a world frame and an independent source hash. Tag IDs are `tag36h11` values from 0 through 586. Duplicate IDs and nonfinite coordinates are rejected.

## Run

```sh
uv run python -m tools.ohmni_world_registration \
  observed_tags.json world_tags.json registration_candidate.json \
  --held-out-tag-id 42
```

The output records source and target frame names, both source provenance records, fitted and held-out tag IDs, the transform, and residuals for every tied tag. The command also records SHA-256 hashes of both input documents. Its `approval_status` remains `unapproved`.

Weighted observation fusion, camera confidence scoring, and drive-over overrides feed later stages. This tool only evaluates explicit point ties.
