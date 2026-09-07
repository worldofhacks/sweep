# Ohmni pose timing

The runtime can publish the encoder update's source timestamp with an odometry
pose after clock qualification. The timestamp is the right-wheel reply that
completes a paired differential-odometry update. The left wheel was read earlier;
its reply interval is retained alongside the pose. This describes when the pose
estimate was updated and does not establish simultaneous wheel acquisition.

Odometry retains the exact integer nanoseconds with its immutable pose snapshot.
Device status carries that snapshot to the runtime, which samples a fresh
publication receipt after reading status. Legacy polling, lost odometry, and stale
snapshots carry no source timestamp. Other observation kinds retain their existing
timestamp behavior.

## Required qualification

Retain evidence for the active robot boot that the vendor Node `process.hrtime`
clock and Android Python `CLOCK_MONOTONIC` share the declared epoch and rate within
measured error. The mapper's ADB clock probe measures host-to-Python alignment;
it does not establish the vendor clock's relationship to Python. Camera mapping
also requires the V4L2 dequeue-to-NUT PTS comparison described in
[the live mapper procedure](ohmni-live-tag-mapper.md).

Record the observed left-to-right reply intervals and the intended maximum pair
skew. A configured skew limit bounds admission. Measured pose error needs separate
qualification. Wheel geometry, transport delay, camera calibration, and mounting retain
their own qualification requirements. If either source uses a different clock,
retain separate source clocks until a measured conversion is implemented.

## Runtime configuration

After qualification, add these arguments to the existing reviewed runtime command:

```sh
--source-clock-id "$QUALIFIED_ROBOT_CLOCK_ID" \
--pose-clock-mapping-id "$QUALIFIED_MAPPING_ID" \
--pose-clock-boot-id "$QUALIFIED_BOOT_ID" \
--maximum-pose-sample-skew-ns "$MEASURED_PAIR_SKEW_LIMIT_NS"
```

The equivalent environment variables are `SWEEP_SOURCE_CLOCK_ID`,
`SWEEP_POSE_CLOCK_MAPPING_ID`, `SWEEP_POSE_CLOCK_BOOT_ID`, and
`SWEEP_MAXIMUM_POSE_SAMPLE_SKEW_NS`. The mapping ID, boot ID, and skew limit must be
supplied together. The skew limit accepts integer nanoseconds from zero through
100,000,000. The CLI checks the boot pin before opening the device. Direct runtime
construction also checks the pin before starting, but a caller may already have
constructed its device.

Configure the relay's `ohmni-pose` adapter binding to allow the same mapping ID and
provide the qualified clock mapping. Camera and tag localization bindings need
their own authorization for that mapping. Fusion requires the same qualified
source clock on the camera and pose observations.

The runtime publishes a pose capture timestamp only when position quality is
positive, the paired sample has exact integer timing, its skew meets the configured
limit, and its age is between zero and 100 ms. Otherwise both capture time and
mapping ID remain null. This leaves the pose available as diagnostic evidence.
All three timing options default to unset. A new boot invalidates the old pin;
this feature does not enable motion or change the runtime's motion guards.
