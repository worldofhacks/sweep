# Marble three-image room capture

Research date: 2026-09-02

## Summary

The proposed room-by-room flow is feasible as a visual MVP. Marble accepts multi-image prompts, and three images fit within both documented input modes. The best match is Auto Layout in the Marble web app or reconstruction mode in the API. The three photos should come from close proximity, face different directions, overlap, and use identical resolution and aspect ratio. World Labs does not publish a minimum image count or a quality guarantee for exactly three images.

Each room becomes its own generated world. Studio Compose can place, rotate, and scale those worlds into one navigable composition. This is manual visual assembly. World Labs does not document automatic registration across rooms, common coordinates, floor-plan alignment, metric accuracy, or an API for Compose. A whole-building walkthrough can be authored in Studio Record and downloaded as MP4, but the camera trajectory and enhanced video do not persist after leaving the Record page.

Marble fills unseen areas creatively. A three-photo result is therefore a generated interpretation of a room, rather than a measured scan. The MVP can promise an AI-generated walk-through that resembles the photographed rooms. It should not promise dimensionally accurate mapping, complete inventory, inspection evidence, or navigation-safe geometry.

The public API supports asynchronous multi-image generation and returns a Marble viewer URL, panorama, Gaussian splats, and a coarse collider mesh. Standard three-image generation costs 1,600 API credits, or $1.28 at the published rate. World Labs estimates about two minutes to create the intermediate panorama from multi-image input and about five minutes to create the final world. Default accounts can start about three generations per minute and 60 per hour; accepted generations may run concurrently.

## Product implication table

| Product decision | Verified capability or constraint | MVP implication |
|---|---|---|
| Take exactly three photos | The UI accepts up to four directed images or up to eight Auto Layout images. The API schema supports multi-image input and reconstruction mode with up to eight images. No minimum is published. | Three images are a valid product constraint, but treat output quality as an experiment with acceptance tests. Allow a retake when the input gate fails. |
| Capture pattern | Auto Layout works best when images come from close proximity, cover different directions, overlap, and share exactly the same resolution and aspect ratio. | Guide the user from one marked standing point. Show three yaw targets and require shared visual features between adjacent photos. Lock orientation, lens, crop, and output dimensions. |
| Image quality | World Labs recommends 1024 pixels on the long side, 16:9 through 9:16, no more than 20 MB, and PNG, JPG, or WebP. A separate image guide says a minimum of 1024 by 1024, which conflicts with the 16:9 recommendation. | Normalize all three images to one supported size and ratio. Use at least 1024 pixels on both axes to satisfy both published statements, subject to the 20 MB limit. Prefer sharp, well-lit frames with visible floor, walls, and ceiling. |
| One room per generation | Multi-image inputs must depict the same space. Marble creates a panorama before generating a world. | Submit one job per room and preserve the three source images beside the returned world ID. Do not combine unrelated rooms in one prompt. |
| Connect rooms | Studio Compose imports multiple saved worlds, then exposes manual XYZ position, rotation, and uniform scale. It advises aligning floors, overlapping boundaries, matching lighting, and testing transitions. | Capture both sides of every doorway as separate composition references; they do not need to be among the exactly three generation inputs. Plan for a human composition step in the first MVP. |
| Whole-building model | The public material documents manual composition. It does not document automatic multi-room registration, shared metric coordinates, joint reconstruction, pose ingestion, or error bounds. The public OpenAPI spec has world generation and asset endpoints, with no Compose endpoint. | Call the result a composed walkthrough or visual world. Metric stitching needs a separate SLAM, photogrammetry, LiDAR, or floor-plan pipeline in a later milestone. |
| Automated product flow | The API accepts multi-image jobs, optional azimuths, and a reconstruction flag. It returns a long-running operation that must be polled. Compose and Record are documented as Studio interfaces. | Room generation can be automated now. Whole-building composition and walkthrough authoring remain operator-assisted unless Sweep builds its own splat scene and camera-path tooling. |
| Browser and app delivery | Each generated world has a `world_marble_url`. Marble supports browser share links and VR links. The API returns SPZ splats, a panorama, and a collider GLB. | The quickest room preview is the returned Marble URL. A branded multi-room viewer can load exported splats with World Labs Spark or another compatible renderer. No official iframe/embed contract was found. |
| Walkthrough video | Studio Record supports keyframed camera paths and MP4 download. Optional enhancement may add detail, remove artifacts, and add dynamic elements. Record keyframes and enhanced video are lost when the page is left. | Make the first walkthrough a manually reviewed export. Save the MP4 immediately. Label enhanced footage as generated because it can introduce visible content and motion. |
| Truth and provenance | World Labs says image prompting may expand beyond the visible source. Directed views without overlap cause creative filling between views. | Keep source photos and room IDs. The UI should distinguish captured photos, Marble-generated room worlds, composed placement, and enhanced video. |
| Mobile capture | Marble runs on mobile web, but World Labs says some advanced creation tools and panorama viewing are unavailable on mobile. | A custom phone capture flow should upload to Sweep's backend and call the World API. Use desktop Marble Studio for composition during the MVP. |
| Latency | Published estimates are about two minutes for a multi-image panorama, 20 seconds for a draft, five minutes for a final world, and up to one hour for a high-quality mesh. | Design generation as a queued job with per-room status, retry, and later notification. Do not block the capture session on each completed world. |
| API cost | API credits cost $1 per 1,250 credits. Standard multi-image generation uses 1,600 credits ($1.28). Draft uses 250 credits ($0.20). Marble 1.1 Plus uses 1,600 to 3,100 credits ($1.28 to $2.48). API billing is separate from the web app. | A ten-room standard run costs $12.80 in generation before retries or optional exports. Use Draft for capture validation only if its geometry proves predictive of the final model. |
| Web-app cost | The current pricing page lists Free at $0 with 7,000 credits, Standard at $20 with 20,000, Pro at $35 with 40,000, and Max at $95 with 120,000. It lists up to 4, 12, 25, and 75 world generations respectively. | Use the web app for manual prototyping. Budget the shipped flow against API pricing because app credits cannot fund API requests. |

## Verified input behavior

### Exactly three images

Three images fit within the documented limits:

- Direction Control accepts up to four images. The operator labels each image Front, Back, Left, or Right. World Labs says missing overlap lets the model creatively fill the gaps.
- Auto Layout accepts up to eight images from the same space. All images need identical dimensions and aspect ratio. Nearby capture positions, different viewing directions, and overlap work best.
- The public API accepts a `multi-image` prompt. Each image may have an azimuth. Its OpenAPI schema exposes `reconstruct_images`; enabling it allows up to eight images, compared with four in the default mode.

World Labs does not state a minimum number in the UI guide or an OpenAPI `minItems` constraint. The official examples use two images. It is reasonable to infer that a three-item request is accepted, but published sources do not establish the resulting coverage or fidelity.

### Capture requirements for the MVP

The capture UI should enforce the requirements World Labs publishes:

1. Keep the user near one position inside the room.
2. Aim the three photos in different directions with visible overlap between neighboring views.
3. Use identical resolution and aspect ratio for all three photos.
4. Keep the scene static and the lighting and color temperature consistent.
5. Reject blur, heavy compression, darkness, overexposure, close-ups, and frames without clear spatial depth.
6. Favor views that include architectural planes and stable features, such as floor-wall corners, ceiling lines, doors, and furniture.

People and animals are currently described as poorly supported. They should be absent during capture. The docs do not publish specific behavior for mirrors, glass, blank walls, moving objects, or repeated textures, so these need product testing rather than unsupported guarantees.

## Public API and Marble Studio split

The public API is sufficient for per-room generation. It can upload local image assets, start a multi-image generation, poll the operation, retrieve the world, and expose its output assets. The generated world includes a Marble URL, SPZ splats at several resolutions, a panorama, a caption, thumbnail, and coarse collider mesh. High-quality mesh generation is a separate operation and cost.

Studio supplies the documented building assembly and presentation workflow:

- Compose adds existing worlds and manually adjusts each scene's XYZ position, rotation, and uniform scale.
- Record creates a keyframed camera path and downloads an MP4.
- Share copies browser and VR links.
- Export provides splats, panoramas, collider meshes, and eligible high-quality meshes.

The public OpenAPI specification does not list Compose or Record endpoints. This limits full automation of the proposed building experience through World Labs alone. Sweep could export each room's splats, place them in a shared Three.js scene using Spark, and generate its own camera path. That would be Sweep integration work, and it would still lack metric room alignment unless another mapping system supplies transforms.

## Drone capture contract

The phone flow and drone flow need different capture patterns. Three phone photos are a sparse onboarding prompt. A drone seeking full room coverage needs a native spherical panorama or enough overlapping frames for the camera's calibrated field of view.

DJI Mobile SDK exposes panorama mode and panorama-shooting state for supported products and cameras. It runs through an Android SDK, while Sweep's existing adapters are Python, ROS 2, and MAVLink. The integration therefore needs a small Android capture bridge that the relay can call; the bridge reads capabilities, triggers the requested operation, downloads media, and returns file acknowledgements for capture association. It must record the exact tested aircraft, controller, camera, firmware, and Mobile SDK combination. A generic SDK symbol does not prove that a particular hardware stack supports the operation.

Support varies by SDK version, aircraft, camera, and firmware, so bring-up must probe capabilities at runtime. When the camera returns a valid stitched equirectangular panorama, Sweep can send that single artifact to Marble. When it returns component frames, Sweep can submit up to eight overlapping images in reconstruction mode. DJI's official panorama tutorial uses eight captures at 45-degree yaw increments and warns that mission output may need manual stitching. [DJI Mobile SDK version differences](https://developer.dji.com/doc/mobile-sdk-tutorial/en/quick-start/version-differences.html) [DJI supported products](https://developer.dji.com/doc/mobile-sdk-tutorial/en/) [DJI panorama tutorial](https://developer.dji.com/mobile-sdk/documentation/ios-tutorials/PanoDemo.html)

A fallback yaw sequence derives its spacing from `yaw_step <= horizontal_fov * (1 - overlap_fraction)`. Forty percent overlap is an initial Sweep experiment. With an 82-degree horizontal field of view, the maximum step is about 49 degrees, so eight headings at 45-degree increments form a sensible first test. A level yaw ring misses the floor and ceiling. Sweep should call an artifact a panorama only when the camera or a verified stitcher returns a full equirectangular image.

Intent v1 therefore needs two confirmed requests before it freezes:

- `capture_room {room_id, capture_id, pattern}` requires exactly one selected aircraft already hovering at an approved pose. The planner owns yaw, gimbal, settle, camera-ready, capture, and file-created steps.
- `map_area {area_id}` resolves a supplied occupancy map, room graph, and approved capture poses; assigns known rooms to the selected swarm; plans collision-checked routes; and schedules room captures. It remains distinct from the lawnmower `sweep` intent.

Every returned file carries its capture ID, aircraft pose, yaw, gimbal pitch, camera intrinsics, timestamp, and file ID. Marble receives those files only after the planner and arbiter complete their work. Marble output never supplies occupancy, clearance, geofence, collision, or positioning truth.

## Multi-room accuracy boundary

Compose can make room worlds look connected. Its documented alignment process is visual: adjust transforms, align ground levels, overlap shared areas, match lighting, and navigate through the transition. World Labs does not publish automatic feature matching across worlds, calibrated camera-pose input, joint optimization, floor-plan constraints, dimensional accuracy, or a multi-room reconstruction benchmark.

Marble's own image guide says the model may expand beyond visible source content. Its multi-image guide says gaps between non-overlapping views are filled creatively. Record's enhanced video may add detail and dynamic elements. These behaviors are useful for a smooth, attractive walkthrough and unsuitable as evidence that an unseen wall, object, distance, or connection existed in the building.

The MVP claim should be: "Create an AI-generated, room-by-room walkthrough from three guided photos per room." The words `scan`, `accurate map`, `digital twin`, and `as-built` imply measurement that this workflow does not provide.

## Failure modes to test

| Failure | Published basis | Detection or mitigation |
|---|---|---|
| Auto Layout misplaces views | World Labs calls out inconsistent aspect ratios, insufficient overlap, poor quality, lighting mismatch, and images from different spaces. | Run client-side dimension checks, blur and exposure checks, and an overlap/similarity gate before upload. Offer guided retake. |
| Invented room regions | Marble creatively fills gaps and expands beyond photographed content. | Preserve the three inputs and disclose generated regions. Do not use the world for measurement or factual inventory. |
| Doorway jump between rooms | Compose depends on manual position, rotation, scale, floor, overlap, and lighting alignment. | Capture doorway overlap from both rooms, add a composition review step, and grade every transition before export. |
| Scale or floor drift across a building | No metric registration or accuracy contract is published. | Use a floor plan or measured anchors for manual placement. Add SLAM or LiDAR in a later mapping milestone. |
| People or pets render poorly | World Labs says characters, humans, and animals are not well supported. | Require an empty, static room during capture. |
| Slow or failed job | Generation is asynchronous, takes minutes, and can return generation errors. | Queue rooms independently, poll operations, expose retry state, and retain uploads and operation IDs. |
| Mobile workflow gap | Some advanced creation and panorama features are missing on mobile web. | Use a purpose-built capture interface and backend API. Reserve Studio for desktop operators. |
| Walkthrough work is lost | Record trajectories and enhanced videos do not persist after leaving the page. | Keep the page open through download and upload the MP4 into Sweep storage immediately. |
| Heavy viewer or export | A room can contain about two million splats; compositions have an account splat limit, with the docs showing 2,000,000. | Test low-resolution SPZ assets and room streaming. Treat the displayed Compose limit as account-dependent until verified on the target plan. |

## Sources

- [World Labs prompt requirements](https://docs.worldlabs.ai/marble/create/prompt-guides)
- [World Labs multi-image prompt guide](https://docs.worldlabs.ai/marble/create/prompt-guides/multi-image-prompt)
- [World Labs image prompt guide](https://docs.worldlabs.ai/marble/create/prompt-guides/image-prompt)
- [World API quickstart](https://docs.worldlabs.ai/api)
- [World API OpenAPI specification](https://docs.worldlabs.ai/api/reference/openapi)
- [World API generation endpoint](https://docs.worldlabs.ai/api/reference/worlds/generate)
- [World API pricing](https://docs.worldlabs.ai/api/pricing)
- [World API rate limits and time estimates](https://docs.worldlabs.ai/api/rate-limits)
- [Marble Studio Compose](https://docs.worldlabs.ai/marble/create/studio-tools/compose)
- [Marble Studio Record](https://docs.worldlabs.ai/marble/create/studio-tools/record)
- [Marble export formats](https://docs.worldlabs.ai/marble/export/specs)
- [Marble sharing FAQ](https://docs.worldlabs.ai/marble/support/faq)
- [Marble platform overview and generation times](https://docs.worldlabs.ai/)
- [Current Marble app pricing](https://marble.worldlabs.ai/pricing)
- [Marble model announcement](https://www.worldlabs.ai/blog/marble-world-model)
- [DJI Mobile SDK version differences](https://developer.dji.com/doc/mobile-sdk-tutorial/en/quick-start/version-differences.html)
- [DJI Mobile SDK supported products](https://developer.dji.com/doc/mobile-sdk-tutorial/en/)
- [DJI panorama tutorial](https://developer.dji.com/mobile-sdk/documentation/ios-tutorials/PanoDemo.html)
