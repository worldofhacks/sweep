# Ohmni live tag mapper

The live mapper turns a decoded camera frame into one camera observation and one raw `camera → tag:<id>` observation for every decoded AprilTag. It does not emit a body pose, an odometry pose, or a map pose. The candidate-fusion step joins these raw observations with separately qualified odometry, lidar, camera mounting, and registration evidence to build an unapproved tag map candidate.

The mapper receives its NUT sidecar from the robot through an ADB reverse tunnel. It creates `adb reverse tcp:<port> tcp:<port>` before opening its host-loopback listener. The robot ffmpeg process still writes to `127.0.0.1:<port>`, which ADB forwards to that listener. The mapper removes the reverse tunnel when its capture finishes or fails. Both ffmpeg tee outputs use `onfail=abort`; loss of the RTSP publisher or NUT sidecar exits ffmpeg. `--sidecar-connect-timeout-s` bounds the time the mapper waits for the robot connection.

Set `--tag-submit-interval-ms` to at least the tag source's relay ingress interval. Frames with several tag observations use that spacing between tag submissions. A camera observation and its tags retain the same capture timestamp and image ID.

## Accepted observation archive

Set `--archive-output evidence/mapping-events` to preserve the relay's accepted observation echoes for the authenticated ground node. The output directory contains canonical `observations.jsonl` and a manifest with the exact session, device, epoch, sources, frames, limits, counts, and SHA-256 digest. It contains accepted camera frames, tag observations, odom-to-body poses, and lidar scans that match the configured source IDs and local frames. The archive is evidence only and has no candidate or approval field.

The default source and frame values match the Ohmni runtime: `ohmni-pose`, `ohmni-lidar`, `odom`, `body`, and `lidar`. Use `--archive-pose-source-id`, `--archive-lidar-source-id`, `--archive-odom-frame`, `--archive-body-frame`, and `--archive-lidar-frame` when the runtime uses different names. `--archive-max-records` is capped at 1024, `--archive-max-bytes` at 10 MiB, and `--archive-duration-s` at 120 seconds. Reaching a bound stops the mapper cleanly and writes the collected evidence with its `stop_reason`. After the NUT stream ends, `--archive-drain-s` reads the same subscription for up to two seconds by default, which collects relay echoes already queued for the active scope.

The archive writes only observations returned by the relay after authenticated admission. It preserves each accepted `t_capture`, source receipt timestamp, clock mapping ID, and relay-assigned `t_ingest`. A missing pose capture timestamp remains null. Tag fusion needs a pose producer with a measured capture association before it can use that pose for a camera frame.

## Continuous collection

Use `--continuous-archive-output` to rotate accepted archives while one mapper connection stays open. Each chunk keeps the configured record, byte, and duration limits. The collector also stops before its raw or unique aggregate reaches 32,768 observations or 64 MiB, the candidate builder's limits. At a boundary, the collector copies the last accepted body-pose observation into the next chunk without changing its event ID, bytes, capture time, or receipt time. `collection.json` pins every raw chunk and declares that one duplicated handoff pose for each boundary. It also records separate hashes and counts for the raw concatenation and the aggregate after removing the declared second handoff copies.

The multi-archive candidate tool accepts this collection file with `--collection`. It validates every archive's pose clock and timestamp order, the exact handoff bytes and timestamps, and the raw and unique summaries. An undeclared duplicate is refused. The handoff replaces only the generic cross-chunk gap check.

The body-pose source uses the paired encoder's right receipt as its pose timestamp. Camera frames retain their V4L2 PTS. Fusion compares those two measurements using the reviewed association bound, which must be no more than 100 ms. The encoder record retains both receipt times and the pair skew; it does not identify either receipt as a camera exposure.

## Host command

After capture qualification, use the session's reviewed configuration and measured
artifacts to fill these variables. `LOCALIZATION_TOKEN` belongs to this ground
node's localization principal. The relay must declare the camera and tag source
IDs, frames, current connection epoch, and `CLOCK_MAPPING_ID`; its clock mapping
must match the robot boot and capture clock. The robot must have a current `registered`, `ready`, or `degraded` connection.
Passive capture works with drive authority disabled.

```sh
timeout --signal=INT --kill-after=5s 90s python -m tools.ohmni_live_tag_mapper \
  --relay-url "$RELAY_URL" --session "$SESSION" --device-id "$DEVICE_ID" \
  --token "$LOCALIZATION_TOKEN" --pts-port 19090 \
  --sidecar-connect-timeout-s 30 --relay-receive-timeout-s 5 \
  --tag-submit-interval-ms "$TAG_INGRESS_INTERVAL_MS" \
  --camera-source-id ohmni-live-camera --tag-source-id ohmni-live-tag \
  --camera-frame camera --camera-serial "$CAMERA_SERIAL" \
  --calibration "$CALIBRATION_FILE" --calibration-sha256 "$CALIBRATION_SHA256" \
  --clock-id "$CLOCK_ID" --clock-mapping-id "$CLOCK_MAPPING_ID" \
  --adb-serial "$ADB_SERIAL" --boot-id "$BOOT_ID" --clock-probes 5 \
  --maximum-clock-error-ms "$CLOCK_ERROR_LIMIT_MS" \
  --maximum-capture-lag-ms "$CAPTURE_LAG_LIMIT_MS" \
  --archive-output "$ARCHIVE_DIRECTORY" --archive-duration-s 30 \
  --archive-max-records 1024 --archive-max-bytes 10485760 \
  --confidence "$CONFIDENCE" --covariance "$COVARIANCE_M2" \
  --tag-sizes "$TAG_SIZES_JSON"
```

For a multi-chunk run, replace `--archive-output` with `--continuous-archive-output "$COLLECTION_DIRECTORY"`, add `--continuous-archive-max-chunks 8`, and pass `--relay-observations-file "$SWEEP_OBSERVATIONS_FILE"`. Continuous collection defaults its pose source to `ohmni-capture-pose`; override it only with the configured capture-pose source. The file must be the exact host-owned file loaded by the relay. The mapper verifies its ground adapter binding, `odom` and `body` declarations, and the same `CLOCK_MAPPING_ID` on the same nanosecond `CLOCK_ID`, then reports the file's SHA-256 in its result. The collection stops after the configured number of chunks or when the NUT stream ends.

`COVARIANCE_M2` is a JSON array of nine values for the qualified translation
covariance. `TAG_SIZES_JSON` maps tag IDs to measured black-square sizes in metres.
The calibration hash is the SHA-256 of the exact calibration file. Clock error,
capture lag, confidence, and covariance must come from qualification. The command
uses a 30-second archive limit, a 90-second host limit, and five seconds for
cleanup. Choose a new `ARCHIVE_DIRECTORY`; an existing destination is refused. Start the reviewed
camera publisher with its NUT output pointed at robot loopback port 19090 after
the host listener starts. The mapper creates and removes the matching ADB reverse
forward automatically.

## Capture timestamp qualification

The mapper queries `CLOCK_MONOTONIC` through the robot's installed Python runtime. Linux `/proc/uptime` includes suspend time, so it cannot replace that query. See [`clock_gettime(2)`](https://man7.org/linux/man-pages/man2/clock_gettime.2.html) and [`proc_uptime(5)`](https://man7.org/linux/man-pages/man5/proc_uptime.5.html).

The NUT reader preserves encoded PTS values. That proves only what the sidecar contained. It does not prove that an Ohmni V4L2 driver supplied those PTS values in the robot boot-monotonic clock domain. Do not use mapper output as qualified capture evidence until the following record exists for the active camera and ffmpeg command.

1. Record the reviewed ffmpeg argument vector. It must show the V4L2 input, `-timestamps default`, `-copyts`, `-fps_mode passthrough`, and the NUT tee leg with `avoid_negative_ts=disabled`.
2. While that ffmpeg process is already reading the active V4L2 node, collect a short read-only `VIDIOC_DQBUF` trace if the device permits it. Preserve each buffer sequence, timestamp, and flags. The trace must show `V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC`; a driver that reports an unspecified or realtime domain needs a different mapping and is refused by this procedure.
3. Preserve the simultaneous NUT sidecar, decoded PTS sequence, process PID, boot ID, and the clock-probe samples used by the mapper. Compare several ordered buffers with the sidecar sequence and deltas. The evidence must show that the sidecar PTS values retain the V4L2 timestamps through ffmpeg encoding and muxing.
4. Repeat after a camera restart and after reconnecting ADB. The boot ID and reverse tunnel must remain the recorded ones. A changed boot ID or failed comparison invalidates the mapping.

This is a planned read-only qualification. It does not authorize opening the camera, attaching a tracer, starting ffmpeg, or running the mapper on a robot. Camera calibration, the camera-to-body transform, odometry and lidar registration, and approval of any resulting tag map remain separate evidence.
