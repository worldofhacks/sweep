# Ohmni local map candidate

`tools/ohmni_local_map_candidate.py` creates one unapproved local-odometry map directory from a completed accepted mapper archive. It snapshots the archive, pinned calibration, pinned measured camera mount, and closed schema-v4 fusion request before recording LiDAR, rendering occupancy, and fusing tags.

The LiDAR config is a reviewed JSON document with `schema_version: 1`, `kind: ohmni_local_map_lidar_config`, the archive's LiDAR source scope and local frames, its measured `mount_id`, bounded recording settings, and grid settings. Pass that same mount ID explicitly:

```bash
uv run python tools/ohmni_local_map_candidate.py \
  --archive accepted-archive/ \
  --lidar-config lidar-config.json \
  --lidar-mount-id measured-rplidar-mount-12 \
  --fusion-request local-fusion-request.json \
  --evidence-root reviewed-evidence/ \
  --output candidates/session-12-local/
```

The command requires accepted LiDAR scans and enough qualified accepted camera, tag, and pose evidence to produce local tags. It rejects changed archive bytes, scope or frame disagreement, a non-local fusion request, mismatched pins, missing LiDAR, and tagless fusion output. The published directory contains the canonical scan recording, Gray8 occupancy PNG, local tag candidate, immutable input snapshots, and hashes for the inputs and outputs.

The candidate remains in the archive's local odometry frame. It has no world registration and does not approve control, flight, or autonomous movement.
