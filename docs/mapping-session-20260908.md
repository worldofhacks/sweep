# Manual mapping session, 8 September 2026

The retained map contains all 53 expected tags: IDs 0–53, with 29 intentionally absent. The final coordinate frame uses tag 38 as its origin, positive X toward tag 39, and positive Z upward. Camera observations cover the corridor and both floor grids; the wall-tag alternatives retain their measurement and fit provenance.

The owner approved this tag-map baseline on 8 September after checking physical measurements. The checkpoint retains the original map bytes and a separate [approval record](../deployments/real-navigation/map-approval.json) tied to their SHA-256. On 9 September, the owner supplied [six wall offsets](../deployments/real-navigation/wall-measurements-20260909.json) from the black top-left corners of tags 19, 33 and 48 and retained the existing height limits. The offsets now produce [entrance wall geometry](../deployments/real-navigation/entrance-wall-geometry.json), world-bundle obstacles and a plan preview. Replacement tags and camera/mount calibration still need hardware verification.

## Retained artifacts

Paths are under `/var/tmp/gauntlet/sweep-production/` on the capture host.

| Artifact | Location | SHA-256 |
| --- | --- | --- |
| Complete tag map | `unit12-live-camera-map-fit-20260908/final-53-tag-map.json` | `7f4f1f2bf3e2ba0285568a7a0a78bd25587fcba82b5b31a97b4c592b00ca2009` |
| Held-out Position 12 camera pose | `unit12-live-camera-map-fit-20260908/unit11-position12-frozen-final-map-pose.json` | `70dad96579e9bd3729dff0256ced81c9bd4fe008aab5cadc53503f0f5c87fa0f` |
| Provisional wall overlay | `unit12-live-lidar-wall-map-20260908/unit11-provisional-tag-wall-overlay.json` | `8ffc9dfdf165d5199fe407a88258a6dc273c5877d859304fe107667071009327` |

The [tag-map snapshot](../deployments/real-navigation/tag-map-53.json) is also retained in the repository. The capture-host map and wall overlay have adjacent PNG previews. The wall overlay contains 43 supported segments from 12 Unit 11 body poses. It retains the ambiguity of raw LiDAR registration and does not approve a new sensor mount.

Validation checked all 95 referenced source hashes, finite numeric values, and the expected tag set. The historical 11-position capture index is preserved as an immutable snapshot. The live index includes the later Position 12 capture. Rebuilding the snapshot references changed artifact hashes while preserving tag geometry, wall-tag choices, camera transforms, and wall segments.

## Agreement with recorded dimensions

Comparing tag-center distances in the final map against the original survey entries gives the differences below. The operator subsequently identified both conflicting floor-distance entries as mistakes and confirmed the provisional map. Keep the fitted geometry and retain those original entries as superseded inputs. No replacement tape readings or measured uncertainty were supplied.

| Dimension | Recorded input | Fitted value | Difference |
| --- | --- | --- | --- |
| Tags 19–37 | 629 in | 671.27 in | +42.27 in |
| Tags 31–0 | 122 in | 226.44 in | +104.44 in |
| Tag 15 center height | 56 in | 58.39 in, unconstrained fit | +2.39 in |
| Tag 18 center height | 59 in | 63.31 in, unconstrained fit | +4.31 in |

The hallway list contains 19 tags, including tag 53, which creates 18 gaps. Eighteen nominal 37-inch gaps total 666 inches, 5.27 inches below the fitted length. The superseded 629-inch length equals 17 such gaps, consistent with an omitted gap in the original count.

The corridor-to-carpet cross-tie differs from the superseded 122-inch entry by 2.65 m. The operator confirmed the provisional map's tag 31–0 placement. The 16–17 baseline sets the fit's scale; its exact agreement is imposed. The selected tag-18 candidate also uses the measured height as a constraint. Neither supplies independent validation.

The recorded inputs are in `office-tag-layout-provisional-20260908/provisional-office-tag-layout.json`; the wall alternatives are in `unit12-live-camera-map-fit-20260908/wall-height-constrained-alternatives.json`, under the same capture-host root as the artifacts above.

## Physical acceptance still required

Retain the approved tag-map baseline and height limits while verifying replacement tags. Combine the generated entrance obstacles with complete route geometry to generate the static flight grid, routes and geofence through the existing bundle tooling. Validate camera intrinsics, the fixed-head mount and world-frame alignment on hardware. Exercise LiDAR guards and stop controls during the map test. The [hardware checkpoint guide](hardware-navigation-checkpoint.md) records the measurements and software verification.
