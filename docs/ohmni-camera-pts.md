# Ohmni camera timestamp sidecar

The optional NUT sidecar shares the camera publisher's V4L2 input and H.264 encoder. Set `SWEEP_CAMERA_PTS_PORT` to an unprivileged TCP port to send a second output to that port on robot loopback. The default camera command keeps its existing RTSP-only behavior.

With the sidecar enabled, ffmpeg preserves input timestamps through `-timestamps default`, `-copyts`, and `-fps_mode passthrough`. Both tee outputs use `onfail=abort`, so a failed RTSP or sidecar output ends that ffmpeg process. The camera supervisor reports loss of fresh output and retries its publisher. Start the receiving listener and any required tunnel before enabling this mode.

`perception.ohmni_pts_capture.NutCaptureReader` decodes exactly one video stream into BGR frames. Each frame retains its PTS converted to integral nanoseconds. Missing, negative, fractional-nanosecond, repeated, or decreasing timestamps are refused. Tests write actual NUT files through PyAV, then run the reader against those files.

`perception.ohmni_clock_probe` bounds the offset between a robot monotonic reading and the host interval around its query. Samples must share a boot ID and have intersecting offset intervals within the configured uncertainty limit. The caller must query the same clock domain that produced the frame timestamps.

Before using a robot capture for mapping, record the active V4L2 buffer timestamp flags and compare ordered buffer timestamps with the decoded sidecar PTS. Confirm that they use `CLOCK_MONOTONIC` and remain consistent after reconnecting. `/proc/uptime` includes suspend time, whereas `CLOCK_MONOTONIC` excludes it, so uptime is unsuitable for that clock query. See the Linux documentation for [proc uptime](https://man7.org/linux/man-pages/man5/proc_uptime.5.html) and [clock_gettime](https://man7.org/linux/man-pages/man2/clock_gettime.2.html).

This source-preservation test and clock estimator do not qualify a particular camera driver. Physical timestamp, calibration, and transport acceptance remain pending for the current Ohmni hardware.
