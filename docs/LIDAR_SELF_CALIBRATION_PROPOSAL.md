# Host-only lidar self-calibration

`tools/ohmni_lidar_self_calibration.py` reads the fixed three-stage capture emitted by the supervised Ohmni 11 runner and writes one new result file. It estimates `SWEEP_LIDAR_OFFSET_DEG` and `SWEEP_LIDAR_ANGLE_SIGN` from the encoder-measured pose changes and raw RPLIDAR revolutions. It does not open a serial device, drive the robot, update configuration, or approve its own result.

The input is schema version 1, kind `ohmni_supervised_lidar_calibration_capture`, for device 11. It has the measured mount, the fixed 152.4 mm wheel diameter, capture limits, nullable boot and executed-bundle pins, and exactly `baseline`, `after_forward`, and `after_yaw` stages. Each stage contains its start pose and paired encoder sample, ten ordered raw revolutions, Linux monotonic start and completion timestamps, measured drift, the encoder-to-revolution maximum time delta, and `monotonic_clock: "linux_monotonic"`. The fitter accepts no additional fields.

The result is an `unapproved_candidate` only after every gate passes. Refused results contain typed reasons and metrics, with no candidate settings. Both outputs retain the capture hash plus the boot and executed-bundle pins. The output path is created with exclusive creation and is never replaced.

## Fit

For raw scan point `r`, the body-frame point is:

```
p_body = R(offset) × diag(1, sign) × r + mount
```

For a capture at pose `i`, relative to baseline pose `0`, the predicted raw-stage to raw-baseline transform is:

```
r_0 = R(sign × delta_yaw) × r_i
    + diag(1, sign) × R(-offset)
      × (R(-yaw_0) × (position_i - position_0)
         + (R(delta_yaw) - I) × mount)
```

The fitter evaluates both signs across the full offset circle, then refines the best offset at 0.1 degree. It scores only the transform predicted by encoder pose and mount. A trimmed symmetric nearest-point residual handles outliers; it does not run free ICP. The first eight revolutions fit the candidate and the final two score it independently.

## Gates

The tool refuses missing motion, unchanged encoder values, sparse points, near-collinear scan geometry, stale encoder-to-revolution timing, drift outside the stage bound, zero odometry quality, poor fit or held-out residual, broad offset uncertainty, a competing sign or offset basin, and incompatible forward and yaw offsets. Scan angles must be in the RPLIDAR 0 through 360 degree range. Invalid or out-of-range raw returns are discarded before fitting.

The synthetic tests cover arbitrary offsets with both signs, the raw-to-body inverse and mount arc, a single-wall ambiguity, bad encoder pose, zero motion, and create-only provenance output. A physical capture remains subject to separate review before any setting reaches the robot.
