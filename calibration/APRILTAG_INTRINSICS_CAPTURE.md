# AprilTag intrinsics capture

Printed `tag36h11` squares can provide intrinsics observations for a fixed camera
and decoding pipeline. Each decoded tag contributes four known, coplanar corners. A set of camera-to-tag views
with different normal directions constrains focal lengths, principal point, and
distortion through the same planar homographies used for a checkerboard.

Use the measured outer black-square edge of **0.199898 m** for every tag from the
current print batch. The operator measured 7.87 in by 7.87 in on 2026-09-08. This
dimension sets metric pose scale. It should be recorded with the capture even though
the homography constraints for intrinsics do not depend on absolute scale.

## Capture

Record through the exact camera, stream resolution, head or gimbal mode, decoder,
and image-processing path that will later supply tag poses. Turn off digital zoom and
any changing stabilization or crop. Either move the camera relative to a fixed tag or hold the tag on a rigid backing
and change its orientation relative to a stationary camera. A single tag is enough; multiple tags may be used if every
tag has the same recorded physical edge or its own recorded edge.

Save one PNG from each of 30 separate, settled holds. Do not choose adjacent video
frames as separate samples. Keep the whole black square visible, sharp, and free of
glare. Aim for a shortest detected edge between 80 and 200 pixels. Frames below
about 60 pixels have little corner precision at this resolution.

Capture five or six views in each of these pose families:

| Family | Tag normal relative to camera |
| --- | --- |
| Front view | Approximately perpendicular |
| Positive pitch | 30 to 45 degrees |
| Negative pitch | 30 to 45 degrees |
| Positive yaw | 30 to 45 degrees |
| Negative yaw | 30 to 45 degrees |

Across those views, place the tag near the centre, left, right, top, and bottom of
the decoded frame, and use two distances. A roll-only turn or translation of a
front-facing tag does not supply the required change of normal direction.

Retain the original recording, the selected PNGs, and a pipeline declaration with
resolution, codec, FPS, camera mode, decoder path, device identity, and independent
horizontal and vertical FOV bounds. Record the tag ID, family, the black-edge value,
its source, and the extraction time for each selected frame.

## Acceptance

Fit only raw decoded corner positions. A pose computed from assumed intrinsics is
not calibration evidence. Require at least 20 distinct image hashes with a full
four-corner detection, a well-conditioned set of square homographies, RMS
reprojection error below 0.5 pixels, and focal estimates within independently
declared FOV bounds.

Reserve at least five held-out pose families or holds. Refit after excluding each
hold and compare focal lengths and the held-out corner residuals. Reject a result
whose focal lengths move materially across those refits, even if its all-frame RMS
is small. Reprojection residual alone cannot establish metric pose accuracy; the
next check is a pose trial against independently surveyed room points.

The fisheye path requires at least 25 views with six validated observed corners
per view. It can use a single printed tag when the raw raster validates its payload
and supplies at least two internal module intersections in addition to the four
outer corners. The extractor refines outer corners against the raster with a
bounded displacement and rejects unsupported module geometry. Candidate selection
validates each visible tag before choosing a view, balances image regions, and
samples across the full capture. The fisheye fit also requires held-out RMS below
0.5 pixels and bounded focal, principal-point, and distortion drift after refitting.

## Unit 12 floor-tag sweep

The 2026-09-08 head sweep retained 49 completed main-camera views across four
capture runs. Two interrupted views were preserved as rejected provenance. The
accepted views span the middle and lower image regions, with valid module tags
mostly at the left and right sides.

The full-span pinhole candidate had 0.273-pixel RMS but focal uncertainties of
64.35% and 20.05%. The fisheye candidate had 0.280-pixel fit RMS and 0.299-pixel
held-out RMS, but refitting changed focal length by 6.59%, principal point by 2.23%,
and distortion by 53.01%, exceeding all three stability limits. Both candidates
remain rejected, and independent FOV bounds for the installed lens are unavailable.
Their homography condition metrics passed; these results show insufficient evidence
for the fitted models, without proving that every head-only sequence is degenerate.
A rigidly backed tag shown at different plane orientations and image locations is
the next acquisition to test.

## Current directional clip

The 615-frame directional clip is useful for showing that the printed tags decode,
for choosing a usable range, and for a diagnostic candidate fit. It does contain raw
corner samples. The candidate rejects 29 separated observations: its 1.829-pixel RMS
exceeds the 0.5-pixel limit, focal standard deviations are 28.8 and 24.8 percent,
and its inferred 48.7-degree horizontal FOV falls outside the independent 60 to
100-degree bound. The homography condition ratio is 0.0279, above the 0.005 floor,
so sharper tilted holds are the missing evidence rather than simply more copies of
these frames. Its 0.323-pixel median residual under the earlier assumed-intrinsics
pose calculation still coexists with a 0.188-m median camera-tag position change
when focal length is varied by plus or minus ten percent.
