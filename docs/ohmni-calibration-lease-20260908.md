Unit 12 completed the four-stage calibration capture after the heartbeat sender changed from 10 Hz to 20 Hz. The 68.165-second run retained all four sets of ten LiDAR revolutions. The mounting fit remains unapproved: its initial held-out residual was 0.285 m.

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

Calibration runner tests passed (40 cases), including the reproduced callback race and real socket timeout. Host tests passed (3 cases), including authentication, bounded closure, and slow-send scheduling. These measurements validate the calibration control path; production route execution has a separate runtime and still requires a qualified mounting transform and map registration.

Offline checks kept the capture unchanged. Stationary first-eight versus last-two scan splits registered at 0.031 m, 0.011 m, 0.051 m, and 0.057 m across the four stages. Both mounting-angle signs and bounded mounting positions were searched. Free registration using the correct raw-angle sign estimated turns near 62 degrees, consistent with the 61.22-degree encoder turn, but retained about 0.135 m trimmed nearest-neighbor error. A separate turn-scale sweep from 0.8 to 1.2 left the original scale as the best fit; its best held-out improvement was only 1.12 mm. These checks support retaining the refusal. They do not establish whether scene overlap, sensor-plane geometry, or another measurement effect causes the remaining error. No odometry or mounting settings were changed.
