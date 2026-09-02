# DJI Mini 3 operator guidance for room capture

## Recommendation

A custom Android app can guide a Mini 3 pilot through room capture. The app runs on the phone connected to the RC-N1, displays the DJI live feed, overlays capture guidance, reads aircraft and camera state, starts a supported capture, downloads the files, and reports progress to Sweep. DJI Mobile SDK 5.18 lists the Mini 3 and RC-N1 as supported at aircraft firmware `01.00.05.00` and controller firmware `04.16.05.00`. DJI's current Android sample also includes an open-source UXSDK that can be copied into a custom app. [MSDK 5.18 release notes](https://developer.dji.com/doc/mobile-sdk-tutorial/en/) [custom-app setup](https://developer.dji.com/doc/mobile-sdk-tutorial/en/quick-start/user-project-caution.html) [official Android sample](https://github.com/dji-sdk/Mobile-SDK-Android-V5)

The first version should provide advisory guidance. Mini 3 exposes processed flight state and a monocular live view, while its aircraft specification lists only downward vision and infrared sensing. It does not provide the directional ranging or shared indoor position required to calculate and validate a collision-safe three-dimensional correction inside an unknown room. The app can tell the pilot which view is missing, whether the aircraft has settled, and whether the camera is ready. A human must choose and approve the hover position until Sweep has an accepted room map, external localization, and clearance sensors. [Mini 3 specifications](https://www.dji.com/mini-3/specs) [MSDK overview](https://developer.dji.com/doc/mobile-sdk-tutorial/en/basic-introduction/msdk-introduction.html)

For one room, first test the Mini 3's native Sphere panorama from a central, clear hover point. DJI lists Sphere, 180-degree, and Wide Angle panorama modes for the Mini 3. MSDK 5 provides generic panorama mode, progress, and shooting-state keys. DJI does not publish a Mini-3-specific key matrix proving that every panorama operation and output artifact works through a third-party MSDK app. The bridge must probe the exact hardware at runtime, download the result, and verify that `pano_360` produced a complete 2:1 equirectangular image. [Mini 3 camera specifications](https://www.dji.com/mini-3/specs) [MSDK CameraKey](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Camera_CameraKey.html)

If the runtime probe cannot deliver that artifact, collect an explicitly labeled multi-image bundle. World Labs says Auto Layout works best with nearby images from the same space, different viewing directions, and visual overlap. Its API also accepts an azimuth for every image. A level ring has no ceiling or floor coverage, so Sweep must keep it distinct from a full sphere panorama. [World Labs multi-image guidance](https://docs.worldlabs.ai/marble/create/prompt-guides/multi-image-prompt) [World API quickstart](https://docs.worldlabs.ai/api)

## Verified DJI capabilities

| Capability | Officially documented | Consequence for Sweep |
|---|---|---|
| Custom Android application | MSDK is an Android library; the RC-N1 connects to the Android device by USB. A registered app key matching the package name is required. Initial registration contacts DJI's server, then caches the registration locally. | Build a purpose-specific phone app from the MSDK V5 sample and rehearse first-run registration before the flight session. [setup](https://developer.dji.com/doc/mobile-sdk-tutorial/en/quick-start/user-project-caution.html) [registration](https://developer.dji.com/api-reference-v5/android-api/Components/SDKManager/DJISDKManager.html) |
| Live view and frames | `ICameraStreamManager` can render a camera stream to an Android `Surface`, provide encoded stream bytes for custom decoding or third-party streaming, and provide decoded frames for algorithm processing. | Put the guidance overlay over the local Surface. Relay the encoded stream to the laptop and use decoded frames locally for blur, exposure, and feature-overlap checks. [camera stream manager](https://developer.dji.com/api-reference-v5/android-api/Components/IMediaDataCenter/ICameraStreamManager.html) |
| Reusable flight UI | DJI's open-source V5 sample composes an `FPVWidget`, primary flight display, horizontal situation indicator, camera controls, system status, battery, and map widgets. | Reuse the small set needed for safe piloting, then add one custom capture-guidance overlay and capture-state panel. [sample layout](https://github.com/dji-sdk/Mobile-SDK-Android-V5/blob/dev-sdk-main/SampleCode-V5/android-sdk-v5-uxsdk/src/main/java/dji/v5/ux/sample/showcase/defaultlayout/DefaultLayoutActivity.java) |
| Aircraft and camera state | KeyManager supports get, set, action, and listener operations. DJI documents position, attitude, velocity, fused downward height, flight state, battery, link, camera, gimbal, storage, capture state, and media retrieval at the platform level. DJI's overview says sensor readings can be available at up to 10 Hz. | Mirror state to the phone and Sweep console. Measure the actual Mini 3 update rate and missing keys before treating any field as a contract. [KeyManager](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/IKeyManager.html) [MSDK overview](https://developer.dji.com/doc/mobile-sdk-tutorial/en/basic-introduction/msdk-introduction.html) |
| Gimbal and camera actions | MSDK exposes gimbal attitude, relative yaw, rotation actions, camera mode, start-photo, panorama state, and panorama progress. MediaManager lists, previews, and downloads stored media. | The phone can show the next heading, start capture after confirmation, display progress, and return checksummed files to Sweep. [GimbalKey](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Gimbal_GimbalKey.html) [CameraKey](https://developer.dji.com/api-reference-v5/android-api/Components/IKeyManager/Key_Camera_CameraKey.html) [MediaDataCenter](https://developer.dji.com/api-reference-v5/android-api/Components/IMediaDataCenter/IMediaDataCenter.html) |
| Mini 3 camera and transmission | The camera has an 82.1-degree published field of view, focus from 1 m to infinity, Sphere/180/Wide Angle panorama modes, a 720p30 controller live view, and approximately 200 ms minimum transmission latency. | Keep important piloting guidance local to the phone. Treat the laptop video as supervisory because encoding, LAN transport, and rendering add latency. [Mini 3 specifications](https://www.dji.com/mini-3/specs) |

The published 82.1-degree value is labeled only as lens field of view. DJI does not label it as horizontal field of view. Capture spacing must use a measured horizontal field of view for the selected aspect ratio and camera mode. Using 82.1 degrees as horizontal field of view would overstate overlap.

## Proposed guidance algorithm

The following is a Sweep product design inferred from the documented inputs. DJI and World Labs do not publish this algorithm.

### M1: unknown room, pilot-approved hover

1. **Probe the node.** Record product, aircraft firmware, RC firmware, phone, Android version, MSDK version, camera mode range, panorama behavior, media behavior, measured horizontal field of view, and stream delay. Refuse unsupported capture patterns.
2. **Choose a candidate hover point.** Ask the pilot to place the aircraft in a clear, central part of the room with broad line of sight to walls, floor, ceiling, and doorways. Keep photographed surfaces beyond the camera's 1 m minimum focus distance. The app labels clearance as operator-approved because Mini 3 cannot verify lateral clearance.
3. **Measure view quality continuously.** Use decoded frames to compute blur, clipped highlights and shadows, feature density, and overlap with the last accepted heading. Read velocity, roll, pitch, yaw, gimbal pitch, link, battery, storage, camera state, and downward height from MSDK where the real-node probe confirms them.
4. **Show a coverage compass.** Divide the measured horizontal field of view into azimuth sectors and mark each sector as unseen, weak, or accepted. The center reticle shows the next target heading and gimbal pitch. Directional arrows mean yaw or gimbal correction in M1. They must not imply a safe XYZ translation.
5. **Gate capture readiness.** Green requires operator-approved pose and clearance, fresh telemetry, acceptable link and battery, writable storage, camera ready, low motion for a short dwell, acceptable exposure and sharpness, and the requested capture pattern supported. The thresholds should come from room trials rather than DJI marketing specifications.
6. **Capture and inspect.** Prefer native Sphere after its hardware probe passes. Display `KeyPhotoPanoramaProgress` while it runs. Download the output, validate file integrity and dimensions, and preview seams and missing vertical coverage. If the result is a set of component frames, stitch and validate it before classifying it as `pano_360`.
7. **Fallback bundle.** For `reconstruct_8`, rotate through headings selected from the calibrated horizontal field of view and desired overlap. Capture only after settle and quality gates pass. Attach measured azimuth, gimbal pitch, aircraft state, timestamp, checksum, and capture ID to each file. Submit up to eight nearby, overlapping images to Marble with their azimuths.

For the sparse three-image experience, use three well-lit views with clear depth, floor, walls, ceiling, and repeated features across adjacent images. World Labs recommends sharp images, consistent lighting, and overlap for Auto Layout. Three images are useful onboarding evidence, while a verified sphere or a denser overlapping bundle gives the room-capture workflow more coverage. [World Labs image guidance](https://docs.worldlabs.ai/marble/create/prompt-guides/image-prompt) [World Labs multi-image guidance](https://docs.worldlabs.ai/marble/create/prompt-guides/multi-image-prompt)

### M2/M3: accepted room geometry and external pose

Once Sweep has a validated occupancy map, room surfaces, a registered external pose, and directional clearance observations, it can calculate capture poses:

1. Erode free space by aircraft radius, guard size, required safety clearance, and localization uncertainty.
2. Sample candidate positions and capture heights in the remaining volume.
3. Ray-cast a calibrated camera or sphere model from each candidate. Score visible wall, floor, ceiling, doorway, and occluded-surface coverage; image overlap; focus distance; feature richness; lighting; clearance margin; and travel cost.
4. Choose the smallest useful pose set with greedy weighted set cover, then order it by collision-checked path cost. Require a second pose when furniture or a doorway leaves important surfaces occluded.
5. Send each approved target in the shared map frame to the phone. The phone may then show metric left/right/forward/up corrections because the transform and its uncertainty are known. The arbiter still revalidates pose freshness and clearance before movement or capture.

This produces two honest guidance modes: `visual_advisory` for M1 and `registered_metric` after the localization gate. The UI should always display which mode is active.

## Display roles

### RC-N1 phone

The phone is the pilot's immediate instrument:

- full-screen low-latency DJI live view;
- flight status, battery, link, RC authority, gimbal state, storage, and camera state;
- a center reticle, coverage compass, next-heading marker, and quality warnings;
- prominent guidance mode and pose-source labels;
- `Ready`, `Capturing`, `Downloading`, `Needs retake`, and `Disconnected` states;
- local cancel and a clear indication that physical RC takeover remains available.

### Sweep laptop console

The console is the mission and evidence view:

- one tile per drone with video, health, room assignment, guidance mode, and readiness;
- a selected-drone detail view mirroring the phone's coverage sectors and next capture;
- room graph or metric map, with candidate and accepted capture poses distinguished;
- capture-bundle thumbnails, missing-coverage warnings, file acknowledgements, and retake requests;
- Marble job state and returned room-world preview after upload.

The phone should publish state and media metadata through the existing relay. M1 uses an explicit laptop button to create and confirm `capture_room` through the planner and arbiter. Language and gesture producers can later emit the same Intent v1 envelope without changing the downstream path. This keeps capture authorization in Sweep while the local phone remains responsive for piloting and camera feedback.

One event can carry the shared presentation state without adding user intents:

```json
{
  "type": "capture_readiness",
  "drone_id": "mini3-1",
  "room_id": "office-101",
  "capture_id": "cap-0042",
  "guidance_mode": "visual_advisory",
  "pose_source": "operator_approved",
  "pose_ok": true,
  "clearance_ok": true,
  "camera_ok": true,
  "motion_ok": true,
  "image_quality_ok": true,
  "coverage_missing": [90, 135],
  "next_heading_deg": 90,
  "suggested_delta": {"kind": "yaw", "degrees": 12}
}
```

In `visual_advisory`, `clearance_ok` records explicit pilot approval and `suggested_delta` is limited to yaw or gimbal guidance. In `registered_metric`, the event may include an XYZ delta plus pose age and uncertainty.

## Hardware acceptance test

Run this before promising Sphere capture through the custom app:

1. Install MSDK 5.18 on the target Android phone and register the exact package key.
2. Connect Mini 3 firmware `01.00.05.00` through RC-N1 firmware `04.16.05.00`.
3. Log `KeyCameraModeRange`, set `PHOTO_PANORAMA` after takeoff, select the Sphere value, start the photo, and record panorama state and progress.
4. Download every new media file and record type, dimensions, size, timestamps, and whether original component images remain available.
5. Confirm whether a valid 2:1 image with full vertical coverage exists. Inspect its seam and compare it with an independent 360 reference.
6. Repeat after application restart, brief link loss, low storage, and aborted capture.
7. Run live view, guidance-frame analysis, telemetry relay, panorama capture, and media download together for 15 minutes while recording phone temperature, throttling, dropped frames, and end-to-end delay.

Until this test passes, the accurate claim is that Mini 3 supports Sphere panorama in DJI's product, MSDK supports Mini 3, and MSDK exposes generic panorama controls. The custom-app combination remains unverified.
