# Ohmni scan evidence tools

`tools.ohmni_scan_record` records the canonical range scans that an Ohmni lidar source receives through the relay, or replays from a bounded JSONL file. `tools.ohmni_occupancy_grid` turns one completed recording into a diagnostic occupancy PNG in the source's local odometry frame. A downstream consumer needs measured registration and its own acceptance before it can use this evidence with `world` or a planner.

The recorder keeps only canonical `range_scan` observations from the configured ground source, epoch, mount, lidar frame, and local odometry frame. It records the source transport as `file_jsonl` or `relay_websocket_console_read_only`, preserves each observation timestamp, and writes a content digest. The file source and relay source both stop at their configured duration, record, and output-byte limits. All input frames count toward the 256 MiB input ceiling, including relay frames the recorder does not select.

The grid uses the full sensor-pose quaternion to project each lidar ray into local XY coordinates. An observation at confidence zero remains in the evidence summary and leaves the grid unchanged. Rays free-space rasterization starts after the clipped visible sensor cell, leaves that first cell unknown, and marks an in-bounds measured endpoint occupied. Empty cells remain unknown. The PNG has at most two million cells and each dimension is at most 4096 pixels.

## Record a local JSONL capture

```bash
uv run python -m tools.ohmni_scan_record \
  --input-jsonl scans.jsonl \
  --session demo-1 --device-id 9 --connection-epoch 3 \
  --source-id ohmni-lidar --odom-frame odom --lidar-frame lidar \
  --mount-id ohmni-lidar-v1 --output evidence/scan-recording
```

The command refuses `world` for either scan frame. The output directory is reserved without replacing an existing path. It contains `observations.jsonl` and `recording.json`; the manifest names the source identity, local frames, input transport, limits, confidence summary, and SHA-256 digest.

## Record from the relay and render a grid

```bash
uv run python -m tools.ohmni_scan_record \
  --relay-url ws://127.0.0.1:8765 --token-file relay-console-token.txt \
  --session demo-1 --device-id 9 --connection-epoch 3 \
  --source-id ohmni-lidar --odom-frame odom --lidar-frame lidar \
  --mount-id ohmni-lidar-v1 --duration-s 120 --output evidence/scan-recording

uv run python -m tools.ohmni_occupancy_grid \
  --recording evidence/scan-recording --output evidence/local-grid \
  --resolution-m 0.05 --bounds -10 -10 10 10
```

`local-grid/manifest.json` records the source-scoped local frame, origin, resolution, PNG row direction, occupancy counts, source identity, and output SHA-256. `occupancy.png` uses 0 for occupied, 255 for free, and 128 for unknown. A grid without `--bounds` derives tight local bounds from observed endpoints. A recording with no endpoints needs explicit bounds.

The tools open input files as bounded regular files without following a final-path symlink. They reject malformed JSON, duplicate observation keys, noncanonical observation lines, digest mismatches, unbounded lines, and source scope changes.
