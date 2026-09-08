# Ohmni camera-inspected pulse admission

`tools.ohmni_camera_inspection` authorizes one 0.04 m/s forward pulse lasting at most 0.5 seconds. The resulting travel is at most 0.02 m. A trusted local operator supplies the inspection decision. The module performs no vision inference and never represents an image as free-space clearance.

The live owner first issues an unpredictable challenge with a device-local monotonic deadline. `tools.ohmni_dual_calibration_capture` accepts that optional challenge and stores it in the root manifest and each frame record. Ordinary captures omit the field. Historical records therefore cannot qualify a new move.

An approval binds the challenge, Unit ID, source boot, positioning source digest, capture-tool digest, exact frame-record digest, PNG and raw-source digests, camera pipeline and collection, current qualified pose, head state, inspected forward capsule, and pulse. The authority consumes the approval once. Pose, head, source, unit, boot, pulse, deadline, or challenge changes refuse it.

The owner calls `InspectionAuthority.consume` only after its lease, head, and full-circle LiDAR checks. It calls the same LiDAR guard on every control tick. Missing, sparse, stale, invalid, or obstacle scans continue to stop motion. Camera approval has no effect on those results.
