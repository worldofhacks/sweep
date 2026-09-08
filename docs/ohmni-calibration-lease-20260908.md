Unit 12’s reviewed planar LiDAR runtime configuration is applied with angle sign −1, offset 131.269876°, and mount x = −0.218548 m, y = 0.155000 m. Normal operation now reads the same nominal 152.4 mm wheel model used during capture. The height setting is 0.5334 m from the operator’s approximate 21-inch measurement. Camera calibration and route validation remain separate work.

The sender now enables TCP_NODELAY and schedules heartbeats against a monotonic clock, so send time does not accumulate into the interval. The node still expires its calibration lease after 350 ms. A separate fix preserves the expiry reason when the lease callback disables the device before the calibration runner inspects its motion result. Failed captures distinguish socket timeout, EOF, rejected renewal, received gaps, and the age of the last renewal without recording tokens.

| Stationary experiment | Sender | Maximum observed arrival gap, first 70 seconds | Unexpected lease loss |
| --- | --- | ---: | --- |
| Idle | 10 Hz | 339.654 ms | No |
| LiDAR and paired encoders | 10 Hz | 240.561 ms | No |
| LiDAR and paired encoders | 20 Hz, TCP_NODELAY | 194.122 ms | No |
| LiDAR, paired encoders, and two captures from each camera | 20 Hz, TCP_NODELAY | 226.210 ms | No |

Each host lifetime was 90 seconds; the table uses the first 70 seconds to exclude intentional termination. On the last run the node reported EOF when the host ended, with a maximum receive gap of 226.193 ms across the full connection. One pair of interleaved diagnostic lines in the first 20 Hz experiment was recovered using its embedded node timestamps; the original log is retained. The probe now serializes diagnostic writes.

The loaded probe used about four percent of one CPU core. Its 10 ms local polling loop never exceeded 12.052 ms. Host routing showed the robot connection traversing a Tailscale subnet router, with about 71 ms TCP round-trip time, a 271 ms retransmission timer, and historical retransmissions. These observations favor transport timing over CPU saturation. The original failed capture had insufficient diagnostics to establish its exact cause, and the experiments do not isolate the contribution of each sender change.

A stationary interruption test withheld a heartbeat for 1.2 seconds. The actual node lease pump expired with `lease_socket_timeout` at 350.592 ms after its last renewal. No wheel control was enabled during this test.

The subsequent physical capture used source commit `2612fededbe4b42ad3eb45ca3cd8ec4e81767e50`, adapter bundle SHA-256 `80e8b004fa229c6c2a9c54924a5008b30715a733ee145a2ec9a950a1693735f3`, and Unit 12 boot `5b54cac3-0e13-4c61-9029-0b8a8c16ee39`. The robot travelled 0.405 m forward, turned 61.22 degrees left, then travelled about 0.411 m along the new heading. The capture SHA-256 is `420d5b4e106ba00cb0c6d9fe832776428281e4274d11f43dcb2c40d95b18d446`.

After completion, the LiDAR serial port had no owner and the remote lease token was absent. Sixteen fresh encoder pairs over about two seconds showed no left-wheel variation and 0.043 mm peak-to-peak right-wheel variation. The host's final BrokenPipeError followed normal node completion before its 90-second lifetime ended.

Calibration runner tests passed (42 cases), including the reproduced callback race and real socket timeout. Host tests passed (3 cases), including authentication, bounded closure, and slow-send scheduling. These measurements validate the calibration control path; production route execution has a separate runtime and still requires camera calibration and map registration.

The next capture, taken beside solid walls, retained another four sets of ten revolutions. Its SHA-256 is `254b4e587b708e2f1e9cac9025d366db728cf4f3c4de41eebf1f612181fbd392`. The general surface fitter at source hash `8e038a305d17` still refused it because its stage estimates disagreed. A visibility correction improved overlap handling, but 32 full resampled surface fits varied by 5.03° in orientation. The retained `full-bootstrap-8e038a305d17/base.json` and `sampling-summary.json` under `/var/tmp/gauntlet/sweep-production/ohmni12-lidar-lines-20260908/` record that replay and every resample. That fitter’s local Hessian remains an objective-shape diagnostic.

A separately reviewed wall fit used only the first eight baseline, forward, and yaw revolutions. It re-extracted two nonparallel walls, projected their points onto common training normals, and solved the planar mount transform. The last two revolutions from those stages and all ten cross-forward revolutions remained outside the fit. Independently extracted held wall points agreed with the predicted transform at 8–25 mm RMS over their shared finite extents; the entirely withheld cross-forward stage measured 19–25 mm across its two walls. Independently estimated line intercepts gave larger translation differences of 25–64 mm.

All 64 whole-revolution resamples repeated wall extraction, correspondence selection, and fitting. Their standard deviations were 0.129° and 1.03/1.52 mm for mount x/y. These describe resampling variation, not total physical accuracy. The actual capture refused the opposite angle sign. A synthetic two-wall fixture recovered its injected mount through the same estimator, and reversing both a line’s normal and distance preserved its inliers.

Deployment review found that the runtime had used its default 150.5 mm wheel model while the capture explicitly used 152.4 mm. `SWEEP_WHEEL_DIAMETER_MM` now reaches the runtime odometry configuration; the default remains 150.5 mm for other nodes. Unit 12 uses 152.4 mm to preserve the pipeline tested by the held observations. The recorded model and held observations define this qualification; physical wheel diameter was not precisely measured.

The installation retained source and configuration backups and left the runtime stopped. A device-side readback verified all seven settings and exercised the installed environment-to-odometry configuration path without constructing a hardware device. Evidence, estimator source, capture, review record, and installation manifests are archived under `/home/gauntlet/sweep-deploy/evidence/unit12-lidar-calibration-20260908/`.

Unit 11 independently completed the same four-stage capture in 68.581 seconds. Its held primary wall aligned within 9–19 mm RMS, but the sparse second wall exceeded the existing 5° association gate in every held target stage and had much larger point residuals. Its mounting fit remains refused, and its runtime configuration remains unchanged.
