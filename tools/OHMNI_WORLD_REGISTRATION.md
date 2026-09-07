# Ohmni world registration

`tools/ohmni_world_registration.py` fits an Ohmni SLAM frame into the world frame from matched tag centers. It creates an unapproved candidate. A tape check or another approval workflow must decide whether the candidate becomes usable.

The solver uses a proper 2D rigid transform only: translation in meters and yaw in radians. It never estimates scale or a reflection. Three or more matched fit tags are required. Fit tags must be noncollinear and have a planar condition number at most 100.

Every fit-tag residual must be at most 0.100 m and RMS residual must be at most 0.075 m. Held-out tags do not participate in fitting; each must be within 0.100 m. The command refuses the candidate when any bound fails, including a single outlier.

## Input

Provide two separate JSON documents. The observed document holds local SLAM coordinates and the W0 source scope that identifies the session, device, epoch, and producer. The known document holds world coordinates plus immutable map pins. Coordinates are in meters and must be within 1,000 km of their frame origin.

```json
{
  "schema_version": 1,
  "frame": "ohmni_slam",
  "scope": {
    "session": "session-42",
    "device_id": 17,
    "connection_epoch": 3,
    "source_id": "ohmni-17-lidar"
  },
  "provenance": {
    "name": "ohmni-scan-2026-09-07",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "tags": [
    {"tag_id": 4, "xy_m": [1.2, -0.4]}
  ]
}
```

The known document must use `"frame": "world"` and add:

```json
"map": {
  "map_id": "lab-a",
  "map_version": "2026-09-07",
  "physical_datum": "tag-0 southwest corner"
}
```

Each document carries a named SHA-256 provenance reference. It records the evidence supplied for the candidate; matching or differing references alone do not establish that a physical measurement is independent. Tag IDs are `tag36h11` values from 0 through 586. Duplicate IDs and nonfinite coordinates are rejected.

## Run

```sh
uv run python -m tools.ohmni_world_registration \
  observed_tags.json world_tags.json registration_candidate.json \
  --held-out-tag-id 42
```

The output records source scope, world-map pins, provenance references, fitted and held-out tag IDs, the transform, and residuals for every tied tag. The command parses and hashes each bounded input snapshot once, then records those two input hashes. Its `approval_status` remains `unapproved`.

Weighted observation fusion, camera confidence scoring, and drive-over overrides feed later stages. This tool only evaluates explicit point ties.
