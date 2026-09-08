# Host-only lidar self-calibration

`tools/ohmni_lidar_self_calibration.py` reads a fixed supervised Ohmni capture and writes one new result file. It estimates the lidar angle convention and, for the multistage profile, a bounded XY mount candidate from encoder-measured pose changes and raw RPLIDAR revolutions. It does not open a serial device, drive the robot, update configuration, or approve its own result.

The input is schema version 1, kind `ohmni_supervised_lidar_calibration_capture`, for device 11 or 12. It has the declared mount, fixed 152.4 mm wheel diameter, capture limits, nullable boot and executed-bundle pins, and the stages required by its immutable profile. The original profiles contain `baseline`, `after_forward`, and `after_yaw`. The multistage 60-degree profile adds `after_cross_forward`, captured after a second 0.40 m translation at the turned heading. Each stage contains its start pose and paired encoder sample, ten ordered raw revolutions, Linux monotonic start and completion timestamps, measured drift, the encoder-to-revolution maximum time delta, and `monotonic_clock: "linux_monotonic"`.

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

The fitter evaluates both signs across the full offset circle, then refines the best offset at 0.1 degree. The multistage profile also searches a bounded 0.50 m XY mount region and reports a candidate only when its joint numerical Hessian is full rank and both mount-coordinate uncertainties are at most 5 cm. The declared mount seeds the search but does not pass through as an accepted result. Each fit and held-out set is median-aggregated into 360 raw-angle bins. Adjacent returns form local segments only when their angle and range are continuous. For the four-stage profile, ray visibility excludes surfaces hidden behind nearer returns before a trimmed symmetric point-to-segment residual retains the closest 80 percent of the supported bidirectional distances. Each direction must retain at least 80 points and half its input points in every fit and held-out stage. Missing overlap produces a finite refusal report. When any stage lacks enough local segments, the scorer preserves one complete revolution from every stage and uses the nearest-point fallback for sparse reflectors. It does not run free ICP. The first eight revolutions fit the candidate and the final two score it independently.

`fit_rms_m` and `held_out_rms_m` report that retained residual. `offset_uncertainty_deg` uses the local score-curvature ratio recorded as `offset_uncertainty_method: "local_curvature_ratio"`; it is an objective-shape diagnostic, not a statistical confidence interval or measured calibration accuracy. Field review remains necessary before any setting is accepted.

## Gates

The tool refuses missing motion, unchanged encoder values, sparse fit or held-out points, near-collinear scan geometry, stale encoder-to-revolution timing, drift outside the stage bound, zero odometry quality, poor fit or held-out residual, broad offset or mount uncertainty, a rank-deficient joint mount fit, a competing sign or offset basin, incompatible stage offsets, and a second translation that is not independent of the first. Scan angles must be in the RPLIDAR 0 through 360 degree range. Invalid or out-of-range raw returns are discarded before fitting.

The synthetic tests cover a known two-wall mount with a changing 120-degree occluded sector, disjoint visibility with finite refusal output, arbitrary offsets with both signs, the raw-to-body inverse and mount arc, a single-wall ambiguity, bad encoder pose, zero motion, and create-only provenance output. A physical capture remains subject to separate review before any setting reaches the robot.
