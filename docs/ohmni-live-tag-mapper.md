# Ohmni live tag mapper

The live mapper turns a decoded camera frame into one camera observation and one raw `camera → tag:<id>` observation for every decoded AprilTag. It does not emit a body pose, an odometry pose, or a map pose. The candidate-fusion step joins these raw observations with separately qualified odometry, lidar, camera mounting, and registration evidence to build an unapproved tag map candidate.

The mapper receives its NUT sidecar from the robot through an ADB reverse tunnel. It creates `adb reverse tcp:<port> tcp:<port>` before opening its host-loopback listener. The robot ffmpeg process still writes to `127.0.0.1:<port>`, which ADB forwards to that listener. The mapper removes the reverse tunnel when its capture finishes or fails. Both ffmpeg tee outputs use `onfail=abort`; loss of the RTSP publisher or NUT sidecar exits ffmpeg. `--sidecar-connect-timeout-s` bounds the time the mapper waits for the robot connection.

Set `--tag-submit-interval-ms` to at least the tag source's relay ingress interval. Frames with several tag observations use that spacing between tag submissions. A camera observation and its tags retain the same capture timestamp and image ID.

## Accepted observation archive

Set `--archive-output evidence/mapping-events` to preserve the relay's accepted observation echoes for the authenticated ground node. The output directory contains canonical `observations.jsonl` and a manifest with the exact session, device, epoch, sources, frames, limits, counts, and SHA-256 digest. It contains accepted camera frames, tag observations, odom-to-body poses, and lidar scans that match the configured source IDs and local frames. The archive is evidence only and has no candidate or approval field.

The default source and frame values match the Ohmni runtime: `ohmni-pose`, `ohmni-lidar`, `odom`, `body`, and `lidar`. Use `--archive-pose-source-id`, `--archive-lidar-source-id`, `--archive-odom-frame`, `--archive-body-frame`, and `--archive-lidar-frame` when the runtime uses different names. `--archive-max-records` is capped at 1024, `--archive-max-bytes` at 10 MiB, and `--archive-duration-s` at 120 seconds. After the NUT stream ends, `--archive-drain-s` reads the same subscription for up to two seconds by default, which collects relay echoes already queued for the active scope.

The archive writes only observations returned by the relay after authenticated admission. It preserves each accepted `t_capture`, source receipt timestamp, clock mapping ID, and relay-assigned `t_ingest`. A missing pose capture timestamp remains null. Tag fusion needs a pose producer with a measured capture association before it can use that pose for a camera frame.

## Capture timestamp qualification

The NUT reader preserves encoded PTS values. That proves only what the sidecar contained. It does not prove that an Ohmni V4L2 driver supplied those PTS values in the robot boot-monotonic clock domain. Do not use mapper output as qualified capture evidence until the following record exists for the active camera and ffmpeg command.

1. Record the reviewed ffmpeg argument vector. It must show the V4L2 input, `-timestamps default`, `-copyts`, `-fps_mode passthrough`, and the NUT tee leg with `avoid_negative_ts=disabled`.
2. While that ffmpeg process is already reading the active V4L2 node, collect a short read-only `VIDIOC_DQBUF` trace if the device permits it. Preserve each buffer sequence, timestamp, and flags. The trace must show `V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC`; a driver that reports an unspecified or realtime domain needs a different mapping and is refused by this procedure.
3. Preserve the simultaneous NUT sidecar, decoded PTS sequence, process PID, boot ID, and the clock-probe samples used by the mapper. Compare several ordered buffers with the sidecar sequence and deltas. The evidence must show that the sidecar PTS values retain the V4L2 timestamps through ffmpeg encoding and muxing.
4. Repeat after a camera restart and after reconnecting ADB. The boot ID and reverse tunnel must remain the recorded ones. A changed boot ID or failed comparison invalidates the mapping.

This is a planned read-only qualification. It does not authorize opening the camera, attaching a tracer, starting ffmpeg, or running the mapper on a robot. Camera calibration, the camera-to-body transform, odometry and lidar registration, and approval of any resulting tag map remain separate evidence.
