# Production main refresh, September 8, 2026

The refreshed integration tree combines the current console and platform baseline with the relay, phone, and device runtime work already on the production integration branch. The reconciliation restored the current Ohmni safety controls after the merge exposed that an older device implementation had displaced them. The tree also keeps the PTS sidecar used by the camera recording path.

The corrected runtime refuses ground drive without measured body and stopping clearance, a current calibrated scan, and a usable pose. It removes drive authority as soon as those checks fail. Hold, emergency stop, return arrival, and completed motion report completion only after the local stop has been confirmed. Ground return release still requires a current signed pose-readiness record.

## Reconciled behavior

- The console supports the current map authoring, route review, media, ground-control, and device flows from the refreshed main baseline.
- The supervised-vertical console profile remains available. Its fixture now records the relay's `requires_home_pose` value, and the relay keeps the qualified voice compiler path available for that profile.
- Ohmni camera publishing retains the PTS sidecar. A failed publisher cleanup now leaves the camera failed and prevents a replacement process; the kill wait is bounded.
- Console map response tests use byte bodies. The former jsdom `Blob` fixture produced a response-body decoding failure in both the integrated tree and a clean checkout of the pinned main revision.

## Validation

The targeted Python validation passed 154 tests across the Ohmni adapter, the supervised console fixture, and ground language behavior. The focused console map suite passed 9 tests; the complete console test command, lint, and production build completed successfully. Android bridge-core ran 192 tests in 19 suites with no failures or errors. Fake and probe debug APKs were assembled from the same tree.

The device and console checks exercise software contracts only. Physical acceptance remains dependent on current camera, encoder, LiDAR, phone-installation, and flight evidence.
