# Unit 12 camera positioning capture

`tools/ohmni_camera_positioning.py` captures fixed-head camera-position evidence for Unit 12. It has four modes: read-only baseline, bounded yaw, a bounded 0.20 m forward capture, and a camera-inspected 0.02 m forward pulse. The tool rejects every other device ID, so Unit 12 measurements cannot be applied to Unit 11.

Forward motion completes within 4 mm of the 0.20 m target. This avoids issuing a final drive command that is shorter than the drive owner’s 100 ms control interval; the artifact records both the measured distance and the terminal tolerance.

The forward mode can pause after a LiDAR obstacle, missing or sparse coverage, stale data, invalid geometry or bins, or a LiDAR read error. The owner writes `<output>.paused.json` for the first pause and `<output>.paused-2.json` for the next one, stops and disables the drive, then retains its odometry, completed stages, encoder progress, lease, and original 60-second deadline in memory.

To resume, run the same command with `--mode forward --resume-from <output>.paused.json`. This invocation sends a request to the still-running owner, which retains the device and odometry frame. The request binds the exact paused-artifact hash, its one-time nonce, Unit 12, the current boot ID, and the tool bundle hash. A request is consumed once.

Before the retained owner sends another pulse, it checks the lease, a qualified unchanged head, its retained pose and progress, and a new clear full LiDAR scan. Restarting the owner process, an expired request, a changed head or pose, a missing owner socket, or an altered artifact leaves the robot stopped. A pause record only authorizes its still-live owner.

The source pin hashes the adapter source bundle, this entry point, and the inspection module. Pass the resulting `camera_positioning_source_sha256()` value as `--expected-source-sha256`.

The inspected-forward pulse uses a 1 mm travel tolerance around its 0.02 m cap. This software guard absorbs the discrete motor command response: the nominal 56-unit command maps to 0.04032 m/s. The final and failed artifacts record the tolerance. It does not establish physical positioning accuracy.

`--mode inspected-forward` stops the owner, writes `<output>.inspection-challenge.json`, and waits up to 30 seconds for a camera review. Copy that file to the capture host. Run `python -m tools.ohmni_dual_calibration_capture` there with `--inspection-challenge-file FILE`, `--device-id 12`, and the challenge boot ID as `--expected-boot-id`.

Copy the root `manifest.json` and the selected camera directory, including its frame record, PNG, and raw source, back to the device. Keep their relative paths. On the device, run `python -m tools.ohmni_camera_inspection --challenge CHALLENGE --frame-record CAPTURE/main/frame-000000.json --manifest CAPTURE/manifest.json --operator-id ID --review-notes TEXT --accept`. It writes `CHALLENGE.approval.json`, which the waiting owner reads. The owner verifies the retained image, raw frame, manifest, challenge, current head, lease, source pin, and LiDAR immediately before one 0.04 m/s pulse lasting at most 0.5 seconds. It stops after that pulse and records the approval in the final or failed artifact.
