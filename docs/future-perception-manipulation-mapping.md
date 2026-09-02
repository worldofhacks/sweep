# Future perception, manipulation, and spatial mapping

Research checked against official platform documentation, open-source repositories, and peer-reviewed papers.

## Recommendation

Sweep can make **operator-confirmed visual search and offline spatial mapping** the concrete M5 goal. Both extend work already planned for M3: timestamped camera streams, detection events, drone poses, recorded sessions, and operator confirmation. M5 should support commands such as “find an energy drink on the counter” and “find candidates matching a red jacket and backpack,” then show ranked evidence. It should also reconstruct a static room and preserve time-indexed observations across runs.

Face detection can support framing and redaction. Face recognition, named-person identification, and persistent biometric tracking should remain outside the roadmap. NIST distinguishes face detection from verification and one-to-many identification, and its evaluations show that image quality and demographic group affect recognition errors. A moving drone adds small faces, blur, occlusion, and steep viewing angles. Description-based person retrieval also conflicts with the current PRD's surveillance non-goal unless the team approves a narrow rescue workflow. Any M5 person-candidate spike must use consented participants in controlled drills, forbid arbitrary bystander tracking, and return temporary session-local candidates without claiming identity. [NIST face-recognition evaluation](https://pages.nist.gov/frvt/html/frvt1N.html) · [NIST demographic-effects report](https://pages.nist.gov/frvt/reports/demographics/nistir_8280.pdf)

A phone can contribute as a hand-held capture baseline, operator client, or sensor package on a larger payload aircraft. It cannot fly on the current Crazyflie-class platform. Crazyflie 2.1 weighs 29 g, flies for about seven minutes on its stock battery, and has a 15 g maximum recommended payload. A Pixel 9a alone weighs 186 g. [Crazyflie 2.1 specifications](https://www.bitcraze.io/crazyflie-2-1/) · [Pixel 9a specifications](https://store.google.com/product/pixel_9a_specs)

“Grab an energy drink from the counter” should become a later manipulation research goal. The first credible flight demo is a 50 to 100 g standardized object, fixed pickup fixture, fiducial pose, soft or passive gripper, larger guarded MAVLink vehicle, and operator confirmation. Published aerial-grasping systems combine RGB-D pose estimation, visual-inertial odometry, trajectory planning, adaptive flight control, and purpose-built grippers. One 2024 system achieved between 6 and 10 successful grasps in 10 trials depending on the 60 to 148 g object. Its aircraft was 1,442 g excluding the 544 g gripper, for about 1.99 kg before the grasped object, and had about three minutes of flight time. [High-speed aerial grasping study](https://www.nature.com/articles/s44182-024-00012-1)

Start with COLMAP Structure-from-Motion and Multi-View Stereo. COLMAP documents this sparse-then-dense workflow and warns that capture needs textured surfaces, stable illumination, high overlap, and translated viewpoints. It recommends downsampling video frames. These constraints suit a deliberate mapping pass better than ordinary mission footage. [COLMAP tutorial](https://github.com/colmap/colmap/blob/main/doc/tutorial.rst)

A phone is most useful as a synchronized sensor package. ARKit exposes captured images with timestamps, camera pose and intrinsics, tracking state, and optional scene depth. ARCore exposes corresponding images, nanosecond timestamps, poses, intrinsics, depth, and per-pixel confidence. ARCore can record video, IMU readings, and application-defined metadata into an MP4 dataset for playback. [ARKit `ARFrame`](https://developer.apple.com/documentation/arkit/arframe) · [ARCore `Frame`](https://developers.google.com/ar/reference/java/com/google/ar/core/Frame) · [ARCore recording](https://developers.google.com/ar/develop/recording-and-playback)

The practical near-term version of "4D" is a spatial map with time-indexed observations, object tracks, and versioned session snapshots. It can answer where an object was seen, what changed between runs, and which frames support a map element. Dynamic NeRF and Gaussian-splatting methods can synthesize deforming scenes, but their published goals and capture assumptions do not establish a safety-grade occupancy map from arbitrary single-drone stills. Keep one bounded research spike after static mapping and temporal change detection pass their gates.

The recommended sequence is:

1. M5: searchable detections, operator-confirmed multi-drone search, static 3D reconstruction, and time-indexed map history.
2. M5 research spikes: phone sensor recording on the ground, neural scene replay, and one controlled dynamic-reconstruction dataset.
3. M6: payload-capable vehicle integration, passive delivery, then constrained pickup of a lightweight standardized object.
4. Later research: a sealed drink can from a purpose-built fixture, followed by general countertop retrieval only if the constrained task is reliable.

## Feasibility decisions

| Idea | Feasibility | Roadmap decision |
|---|---|---|
| Detect people and common objects | High | M3 foundation and M5 search input |
| Find a described object such as an energy drink | Medium | M5 ranked candidates; local evaluation or product-specific training required |
| Find a person matching visible clothing and carried-object descriptors | Medium, with product-risk gate | Controlled rescue drill only after explicit scope and privacy approval |
| Maintain one person's track across drones | Medium to hard | Late M5 after calibrated time, pose, and camera geometry are proven |
| Detect faces for framing or redaction | High | M5 privacy utility; store no face templates |
| Identify a named person by face | High-risk and outside current product scope | Keep excluded |
| Narrate or accept instructions on the operator's phone | High | M5 mobile client or existing browser path |
| Carry a phone on a Crazyflie | Infeasible | Exclude for this airframe |
| Carry a phone on a payload-class vehicle | Feasible with flight penalties | Ground and tethered spike before M6 flight acceptance |
| Build a static 3D room map from planned image capture | Medium pending one measured room baseline | M5 offline deliverable if the baseline registers and meets scale gates |
| Preserve temporal evidence and detect changes between runs | Medium | M5 after static alignment passes |
| Produce a dynamic NeRF or 4D Gaussian scene | Research-grade | Bounded spike; never the authoritative safety map |
| Retrieve a lightweight object from a fixed fixture | Medium to hard | M6 |
| Retrieve an arbitrary drink from a counter | Hard research problem | Later goal after constrained pickup |

## Visual search and person-candidate retrieval

The ground station should run the primary perception pipeline. The planned upper bound of six streams sampled at 5 to 10 frames per second would produce 30 to 60 frames per second for detection. M3 currently accepts one source first, so synchronized 4-to-6-source ingestion, color fidelity, frame loss, and radio coexistence are prerequisites for multi-drone search. Once that gate passes, a compact fixed-class detector can run continuously, trackers can bridge detector intervals, and an open-vocabulary model can evaluate selected keyframes or crops after a query arrives. The 4.4 g Crazyflie AI-deck uses a monochrome camera and exposes hardware plus development resources. It does not supply an out-of-box video product, so color-description claims require a separate proven color stream. [Bitcraze AI-deck](https://www.bitcraze.io/products/ai-deck/)

Grounding DINO accepts an image and text prompt and returns phrase-associated boxes. It is a credible prototype for “energy drink can” or “person with a red jacket,” but its published zero-shot results do not establish performance on Sweep's rooms, product packaging, aerial views, or people descriptions. Those need a consented local dataset. [Grounding DINO repository](https://github.com/IDEA-Research/GroundingDINO) · [Grounding DINO paper](https://arxiv.org/abs/2303.05499) YOLO-World offers a prompt-then-detect alternative with several published checkpoint sizes and deployment paths. Select exact checkpoints only after measuring parameters, memory, and latency on the ground station. [YOLO-World repository](https://github.com/AILab-CVC/YOLO-World) · [CVPR 2024 paper](https://openaccess.thecvf.com/content/CVPR2024/papers/Cheng_YOLO-World_Real-Time_Open-Vocabulary_Object_Detection_CVPR_2024_paper.pdf)

Text-based person search is a retrieval task. The CUHK-PEDES work ranks gallery images against descriptions and produces candidate images without an identity claim. That matches the required product behavior: show top candidates, evidence, score, time, and location. [Person Search with Natural Language Description](https://openaccess.thecvf.com/content_cvpr_2017/html/Li_Person_Search_With_CVPR_2017_paper.html)

Cross-camera continuity should combine appearance, time, camera geometry, and drone pose. Appearance-only re-identification benchmarks do not represent moving aerial cameras. UAV tracking research documents the effect of camera motion and shows gains from flight metadata. [Multi-Object Tracking Meets Moving UAV](https://openaccess.thecvf.com/content/CVPR2022/html/Liu_Multi-Object_Tracking_Meets_Moving_UAV_CVPR_2022_paper.html) · [Metadata-guided UAV re-identification](https://openaccess.thecvf.com/content/WACV2024W/MaCVi/html/Yang_Sea_You_Later_Metadata-Guided_Long-Term_Re-Identification_for_UAV-Based_Multi-Object_Tracking_WACVW_2024_paper.html)

```text
timestamped streams + calibrated drone poses
        |
        v
fixed-class detector -> per-camera tracks -> query-specific ranking
        |                                      |
        +---- world position and time ----------+
                                               v
                                  ranked candidate event
                                               |
                                  operator confirmation
                                               |
                                      planner and arbiter
```

Candidate events should contain the source frame, crop, bounding box or mask, prompt, model version, score, time, drone pose, estimated world position and uncertainty, and a random session-local track ID. They should never contain a claimed identity. Raw video and embeddings need session-scoped retention, restricted access, logged deletion, and a deployment review before operation around bystanders. Arbitrary bystander tracking and biometric galleries remain prohibited.

## Phone sensing and narration

A phone provides camera frames, IMU, storage, networking, speech APIs, and depth on supported devices. Android camera analysis exposes frames to application code; sensor events use monotonic nanosecond timestamps; ARCore can add pose and depth. [CameraX image analysis](https://developer.android.com/media/camera/camerax/analyze) · [Android sensor timestamps](https://developer.android.com/reference/android/hardware/SensorEvent) · [ARCore Depth](https://developers.google.com/ar/develop/depth) · [Android speech recognition](https://developer.android.com/reference/android/speech/SpeechRecognizer) · [Android text-to-speech](https://developer.android.com/reference/android/speech/tts/TextToSpeech)

The safest first use is a mobile Sweep client held by the operator or a hand-held capture run. It can accept speech, show candidates and maps, and play text-to-speech while leaving the aircraft unchanged. A vehicle-mounted phone should begin as a powered-off fit and balance check, followed by local recording on a stationary rig, tethered hover, and slow guarded waypoints on a payload-class vehicle. Published drone-audio work treats ego-noise reduction as a prerequisite for useful onboard audio, so operator speech should continue through a handset or headset. Onboard audio can remain experimental evidence. [Drone audition under ego-noise](https://link.springer.com/article/10.1186/s13636-025-00425-2)

If the goal is to speak instructions to a person near the vehicle, use a purpose-built lightweight speaker and test intelligibility against rotor noise. A full phone is unnecessary for that function. Playback should require an operator-approved message, use a bounded phrase set during early tests, and remain separate from flight control.

## Aerial manipulation

Manipulation adds a new vehicle capability and safety contract. PX4 supports gripper commands and acknowledges payload-delivery actions, which gives Sweep a suitable adapter path on a larger MAVLink aircraft. Target pose, approach, grasp planning, contact dynamics, grasp verification, altered center of gravity, and recovery remain Sweep integration work. [PX4 package delivery](https://docs.px4.io/main/en/advanced/package_delivery) · [PX4 payload use cases](https://github.com/PX4/PX4_user_guide/blob/main/en/payloads/use_cases.md)

The manipulation vehicle should publish explicit capabilities and measured limits: maximum payload, loaded and unloaded flight time, gripper type, grasp confirmation signal, center-of-gravity envelope, minimum approach clearance, and allowed object classes. `pickup` should compile into a staged plan with operator confirmations before approach and grasp. Every stage passes through the planner and arbiter, with aborts for stale target pose, low confidence, slip, low battery, link loss, or positioning loss. Tests need a person exclusion zone, propeller and contact protection, a capped approach speed, emergency flight termination, dropped-payload containment, and a rule forbidding pickup, transit, or delivery over people.

A sealed drink can is far beyond the current airframe's payload. Red Bull lists 250, 355, and 473 ml cans and identifies water as the main ingredient. A 250 ml can therefore contains roughly 250 g of liquid before the can itself, which already exceeds the current platform by more than an order of magnitude. This is an inference from the published volume and composition. [Red Bull product sizes](https://www.redbull.com/gb-en/energydrink/products/red-bull-energy-drink)

The staged manipulation evidence should be:

1. Manually loaded 50 to 100 g surrogate delivered to a marked landing pad.
2. Passive hook or latch release with success feedback.
3. Pickup of a fixed foam cylinder or empty can from a purpose-built fixture using a fiducial and soft or enveloping gripper.
4. The same constrained object without the fiducial after pose estimation is measured independently.
5. A sealed 250 ml can on a vehicle whose weighed payload stack and thrust margin pass acceptance.
6. General countertop retrieval with clutter, obstacle-aware approach, grasp recovery, and operator confirmation.

## Consolidated roadmap

### M5.0: Evidence and calibration contract

Freeze frame, pose, intrinsics, clock, calibration, detection, track, and map-artifact records. Require replayable session bundles before adding search behavior.

### M5.1: Fixed-class search

Search for person, bottle, can, cup, backpack, and phone in recorded Sweep footage. Exit at 90 percent event recall, at most one false alert per five minutes, and detection-to-console p95 under one second.

### M5.2: Descriptive object and person candidates

Support a bounded object-query grammar: object type, color, room or zone, and nearby landmark. Exit at 90 percent top-three recall over 100 randomized object searches, including at least 25 target-absent trials.

Run person-candidate retrieval only after the product-risk gate above. Use at least 100 consented trials: 60 target-present, 20 target-absent, and 20 with similar-clothing distractors but no target. Require at least 75 percent top-one accuracy and 90 percent top-three recall on target-present trials, with alerts in at most 10 percent of target-absent trials. Label every result “candidate.”

### M5.3: Static spatial baseline

Complete MAP.0 and MAP.1 below. Exit with a repeatable metric room reconstruction aligned to the Sweep world frame. A supplied surveyed floor plan may satisfy the spatial prerequisite for search trials while this work is in progress.

### M5.4: Operator-confirmed multi-drone search and map history

After the multi-stream prerequisite passes, partition an accepted map or supplied floor plan across 4 to 6 drones. A match promotes a feed and map marker. Unconfirmed detections emit zero flight commands. Confirmed inspection follows the planner and arbiter, maintains a defined stand-off distance, and stays inside the geofence. Exit when at least 18 of 20 randomized searches succeed and five consecutive swarm runs complete safely. Complete MAP.2 and MAP.3 for pose-aided geometry and temporal history.

### M5.5: Cross-camera continuity and research spikes

Evaluate cross-camera continuity in 20 five-minute consented runs with at least four participants, two camera handoffs, one full occlusion, and a similar-clothing distractor per run. Report IDF1, switches, handoff latency, target-absent behavior, and recovery. Advance only if IDF1 is at least 85 percent and each target averages no more than one switch per run. Also evaluate hand-held phone capture, one neural replay, and one dynamic scene. These artifacts remain outside flight control and do not block the M5 exit.

### M6: Payload and manipulation

Select a payload-class MAVLink vehicle, prove loaded flight behavior, add passive delivery, then attempt constrained pickup.

- Platform gate: weigh the complete payload stack, stay within the manufacturer's rated envelope, measure loaded and unloaded flight time, and pass five guarded waypoint runs without a position, thermal, power, or attitude fault.
- Passive-delivery gate: complete 19 of 20 marked-pad deliveries with correct release acknowledgement and no hard landing.
- Constrained-pickup gate: complete at least 16 of 20 approach, grasp, stable-hover, carry, and release trials on the standardized object; every induced stale-pose, slip, low-battery, and link-loss case must produce the expected abort.
- Sealed-can gate: repeat the full weight, thrust, controller, and safety evaluation on a purpose-built fixture. General countertop retrieval remains conditional on repeated sealed-can success.
- Occupied-space gate: enforce the exclusion zone, approach-speed cap, flight-termination path, and no-flight-over-people rule in every test; dropped payloads remain inside the containment area.

## Approach comparison

| Approach | Best Sweep use | Key constraint |
|---|---|---|
| Photogrammetry, SfM, and MVS | Offline metric room reconstruction | Mostly static scene, overlap, texture, sharp images, and viewpoint translation |
| Visual-inertial SLAM | Online pose, capture guidance, and relocalization | Calibrated camera and IMU timing/extrinsics; drift still needs loop closure or external reference |
| RGB-D fusion | Fast metric geometry from a depth-capable phone | Aligned depth, color, poses, and confidence; depth range and resolution vary by device |
| NeRF or 3D Gaussian splatting | Photorealistic scene replay | Rendering quality does not establish metric surface accuracy |
| Dynamic NeRF or 4D Gaussian splatting | Controlled research demo | Dense time-indexed observations and method-specific scene assumptions |

ORB-SLAM3 supports monocular, stereo, RGB-D, visual-inertial, and multi-map SLAM, with examples for the EuRoC drone and TUM-VI datasets. It is a candidate when trajectory estimation and visual-inertial localization are primary. [ORB-SLAM3 repository](https://github.com/UZ-SLAMLab/ORB_SLAM3) · [peer-reviewed paper](https://doi.org/10.1109/TRO.2021.3075644) RTAB-Map provides RGB-D, stereo, and lidar graph-based SLAM with loop closure, map optimization, ROS 2 packages, and map export. It is a candidate when aligned depth and dense metric mapping are primary. [RTAB-Map API overview](https://github.com/introlab/rtabmap/blob/master/doxygen/mainpage.md)

When reliable depth and poses are available, Open3D can integrate RGB-D frames into a Truncated Signed Distance Function volume and extract a mesh. Its TSDF pipeline reduces depth noise by integrating multiple observations at known camera poses. [Open3D TSDF integration](https://open3d.org/docs/release/tutorial/t_reconstruction_system/integration.html)

Nerfstudio requires camera poses for custom images or video and uses COLMAP for those inputs; it can also ingest phone scanner exports that already contain poses. [Nerfstudio custom data](https://github.com/nerfstudio-project/nerfstudio/blob/main/docs/quickstart/custom_dataset.md) The original NeRF and 3D Gaussian Splatting work target novel-view synthesis. They are useful replay experiments after the metric baseline succeeds. [NeRF paper](https://arxiv.org/abs/2003.08934) · [3D Gaussian Splatting paper](https://repo-sam.inria.fr/fungraph/3d-gaussian-splatting/)

## Capture contract

Every mapping run should create one immutable session bundle:

```text
mapping-session/
  manifest.json
  frames/
  depth/
  trajectory.jsonl
  telemetry.jsonl
  calibration/
  reconstruction/
  reports/
```

The manifest records the device, image dimensions, coordinate frames, clocks, software versions, and checksums. Each frame record carries a monotonic capture timestamp, intrinsics and distortion, exposure metadata when available, camera pose and tracking state when available, optional depth and confidence, and the nearest drone telemetry timestamp. Calibration contains camera intrinsics, camera-to-body extrinsics, and camera-to-IMU extrinsics when VIO is used. Derived poses, meshes, labels, and splats remain versioned products that reference immutable frame IDs.

ARKit's camera transform uses a right-handed local world coordinate system. ARCore also returns session-local camera poses. Sweep must estimate and record a transform into its positioning frame. [ARKit camera transform](https://developer.apple.com/documentation/arkit/arcamera/transform) · [ARCore camera](https://developers.google.com/ar/reference/java/com/google/ar/core/Camera) Google's raw-depth documentation reports typical resolutions around 160 by 120, some devices up to 640 by 480, and optimal accuracy from 0.5 to 5 metres. Depth includes a confidence image and may be unavailable under poor lighting, occlusion, or insufficient motion. [ARCore raw depth](https://developers.google.com/ar/develop/java/depth/raw-depth)

On LiDAR-equipped Apple devices, RoomPlan can export USD or USDZ with recognized room components and dimensions. A hand-held RoomPlan scan is a useful comparison baseline. Surveyed landmarks or an independently measured scan remain the accuracy reference. [Apple RoomPlan](https://developer.apple.com/augmented-reality/roomplan/)

## Temporal mapping and "4D"

Preserving timestamps enables temporal queries, but a dynamic model still needs enough views to separate camera motion, occlusion, and scene deformation. Nerfies reconstructs deformable scenes from casual phone captures using a learned deformation field; its evaluation used synchronized two-phone captures for held-out views. [Nerfies, ICCV 2021](https://openaccess.thecvf.com/content/ICCV2021/html/Park_Nerfies_Deformable_Neural_Radiance_Fields_ICCV_2021_paper.html) Dynamic 3D Gaussians reports dynamic novel-view synthesis and dense 6-DoF tracks, while its released workflow includes scene-specific assumptions such as a known ground plane. [project](https://dynamic3dgaussians.github.io/) · [implementation](https://github.com/JonathonLuiten/Dynamic3DGaussians) The official 4D Gaussian Splatting custom-data instructions still begin with COLMAP pose and dense-point-cloud preprocessing. [CVPR 2024 paper](https://openaccess.thecvf.com/content/CVPR2024/papers/Wu_4D_Gaussian_Splatting_for_Real-Time_Dynamic_Scene_Rendering_CVPR_2024_paper.pdf) · [implementation](https://github.com/hustvl/4DGaussians)

Sweep should first align repeated static maps, retain each session snapshot, associate confirmed detections with position and time, and compute candidate added, removed, or moved regions. Any dynamic neural renderer should consume the same evidence while remaining separate from the authoritative map.

## Mapping work packages and gates

### MAP.0: Capture and calibration

- 100 percent of exported images have unique monotonic timestamps, intrinsics, and explicit coordinate frames.
- At least 98 percent of expected frame intervals are present in a five-minute recording; every gap is reported.
- Camera-to-body calibration repeats within 5 mm translation and 1 degree rotation across three remounts, or the mount becomes fixed.
- Payload mass, centre-of-gravity shift, battery-time change, vibration, and thermal behavior are measured before powered flight.

Visual-inertial calibration must include intrinsics, camera-to-IMU transforms, and temporal offsets. Kalibr's capture guidance calls for short exposures and motion that excites every IMU axis. [Kalibr guide](https://github.com/ethz-asl/kalibr/wiki/Calibrating-the-VI-Sensor) The TUM-VI and EuRoC datasets show the evaluation pattern: synchronized camera and IMU data, explicit calibration, and external pose ground truth. [TUM-VI](https://cvg.cit.tum.de/data/datasets/visual-inertial-dataset) · [EuRoC](https://projects.asl.ethz.ch/datasets/euroc-mav/)

### MAP.1: Offline static room map

- At least 90 percent of selected images register into one COLMAP reconstruction.
- Median error over ten surveyed landmarks is at most 5 cm; the 95th percentile is at most 10 cm after one documented world-frame alignment.
- At least 90 percent of occupied 10 cm surface voxels in the independently measured reference that are visible from the accepted camera poses appear in the point cloud or mesh.
- Two repeated maps align with a median surface distance of at most 5 cm across unchanged regions.
- The report lists unregistered frames, disconnected models, processing time, memory, and tool versions.

These numbers are proposed product gates. COLMAP does not guarantee them. Revise the gates after three baseline room captures.

### MAP.2: Pose-aided RGB-D map

- Every fused depth frame has matching color, intrinsics, pose, timestamp, and tracking-quality data.
- Stale or low-confidence depth is excluded and counted.
- Absolute trajectory RMSE is at most 10 cm in the measured room, with terminal drift at most 2 percent of route length.
- The Open3D mesh meets the MAP.1 landmark and repeatability gates.
- Live blur or tracking warnings appear within 500 ms and never emit a flight command.

### MAP.3: Temporal map history

- Cross-session alignment over unchanged surveyed landmarks has a median error of at most 5 cm.
- Added and removed objects at least 20 cm on their shortest dimension reach at least 90 percent precision and recall across 20 randomized controlled layouts.
- Every change links to before and after frames, timestamps, poses, and confidence.
- Session replay produces byte-identical change-event JSON and numerically equivalent geometry within a declared tolerance.

### MAP.4: Optional neural replay and dynamic spike

- A NeRF and Gaussian-splatting baseline train from the accepted MAP.1 bundle and report held-out PSNR, SSIM, LPIPS, time, GPU memory, model size, and render rate.
- Geometry exports are measured against the MAP.1 landmarks before anyone calls them maps.
- One controlled dynamic dataset has a held-out view and measured target trajectory before training.
- A dynamic method advances only if median target-position error is at most 20 cm and held-out rendering improves over the static baseline.

## Architecture boundary

```text
camera payload or phone capture rig
        |
timestamped session recorder <--- telemetry and positioning
        |
        +--> COLMAP SfM/MVS --------> metric point cloud or mesh
        +--> AR pose/depth + Open3D -> metric RGB-D mesh
        +--> Nerfstudio ------------> optional visual replay
        +--> session aligner --------> snapshots and change events
```

Mapping consumes recorded media, telemetry, and calibration after the existing safety path logs them. It returns versioned map artifacts and evidence-linked events. Planner or arbiter behavior should remain independent until a separate safety review defines freshness, uncertainty, failure semantics, and a deterministic occupancy interface.

Record locally during early runs. Streaming high-resolution video, depth, and metadata would add radio load while the core MVP already carries control and live-video traffic. Upload the complete session after landing, then add low-rate capture-health telemetry if operators need it.

## Decisions before scheduling

- Select the payload and verify high-resolution frame, raw-depth, and per-frame pose export on the exact device.
- Prove a selected payload-class drone can safely carry the phone, mount, and power lead with acceptable flight time.
- Choose the authoritative world frame and the procedure for estimating camera-to-world and camera-to-body transforms.
- Choose the primary deliverable: metric mesh, future-planning occupancy map, photorealistic replay, or a measured combination.
- Define retention and redaction before recordings include bystanders, faces, screens, or private interiors.
