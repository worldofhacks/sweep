# Unit 12 camera positioning capture

`tools/ohmni_camera_positioning.py` captures fixed-head camera-position evidence for Unit 12. It has three modes: read-only baseline, bounded yaw, and a bounded 0.20 m forward capture. The tool rejects every other device ID, so Unit 12 measurements cannot be applied to Unit 11.

Forward motion completes within 4 mm of the 0.20 m target. This avoids issuing a final drive command that is shorter than the drive owner’s 100 ms control interval; the artifact records both the measured distance and the terminal tolerance.

The forward mode can pause after a LiDAR obstacle, missing or sparse coverage, stale data, invalid geometry or bins, or a LiDAR read error. The owner writes `<output>.paused.json`, stops and disables the drive, then retains its odometry, completed stages, encoder progress, lease, and original 60-second deadline in memory.

To resume, run the same command with `--mode forward --resume-from <output>.paused.json`. This invocation sends a request to the still-running owner, which retains the device and odometry frame. The request binds the exact paused-artifact hash, its one-time nonce, Unit 12, the current boot ID, and the tool bundle hash. A request is consumed once.

Before the retained owner sends another pulse, it checks the lease, a qualified unchanged head, its retained pose and progress, and a new clear full LiDAR scan. Restarting the owner process, an expired request, a changed head or pose, a missing owner socket, or an altered artifact leaves the robot stopped. A pause record only authorizes its still-live owner.

The source pin hashes both the adapter source bundle and this entry point. Pass the resulting `camera_positioning_source_sha256()` value as `--expected-source-sha256`.
