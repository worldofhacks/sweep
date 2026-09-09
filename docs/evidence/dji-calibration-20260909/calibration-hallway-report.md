# DJI calibration and handheld hallway check — September 9, 2026

**Owner-approved for the POC.** The owner explicitly qualified this calibration for the POC after reviewing these results; see `poc-approval.json`. The camera intrinsics are promising for further offline testing. This hallway recording does **not yet demonstrate reliable hover localization**. The recording is complete; no aircraft motion commands were sent.

## Calibration

- 20 frozen fitting holds of flat spare tag 54, outer black edge 0.199898 m, 1280 × 720 received video.
- Fitting RMS: **0.152 pixels**. Three separate held-out views: **0.205 pixels RMS**, worst view 0.250 pixels.
- Removing any single fitting hold changes focal lengths by less than **0.8%**.
- Only three of five planned validation holds were collected; pitch and image-edge coverage remain incomplete. Four-corner pose residuals cannot establish physical position accuracy.
- The candidate remains **rejected for qualification** because independent FOV bounds are missing. Camera identity/pipeline provenance, measured body/gimbal extrinsics and exposure timing are also unresolved for flight use. Nothing was admitted to the flight calibration registry.

## Real hallway recording

Received stream recorded from approximately **04:34:01–04:39:01 UTC**, including the real handheld route. Saved 6,592 decoded frames' worth of received video and 302 full-resolution PNG samples; route analysis uses 231 samples after the hallway instruction. Tags appeared in 75 route samples, including 38 distinct IDs. Damaged tags 35 and 49 were excluded from pose analysis, and tag edges below 60 pixels were excluded. Counts include time when the camera was not facing tags and are not a tracking-availability measurement.

Across 19 frames with multiple usable mapped tags:

| Check | Result |
| --- | --- |
| Raw independent-tag camera-position disagreement | Median **8.5 cm**; maximum **2.62 m** |
| After existing single-tag geometry/ambiguity checks | 60 pair comparisons: median **7.9 cm**, 95th percentile **17.6 cm**, maximum **19.6 cm** |
| Existing joint geometry checks with all usable mapped tags | **5 frames pass; 14 fail** |

The filtered result uses the repository's existing geometry routines, including ambiguity rejection and a 2-pixel reprojection threshold. It is a separate offline diagnostic, not a full qualified localization/control run. The raw outliers remain preserved. Pair comparisons share frames and are not independent trials.

These distances measure **agreement between tag-derived camera positions**, not error against measured ground truth. Map geometry, image blur/corner detection and planar pose ambiguity can contribute alongside calibration. No measured camera checkpoints or matched return position were supplied, so absolute position accuracy and return drift cannot be established.

## Evidence and next step

Original received H264, RTP/receipt timing indexes and the complete PNG captures remain in local storage. This Git package includes their capture manifests and raw-video hashes, all 20 fitting and 3 validation PNGs, and hallway corner/pose measurements. Unselected images, raw video and timing indexes are omitted from Git. The receiver stream is not aircraft SD footage and cannot recover packets lost before receipt. The hallway recorder ended at its configured duration; the shutdown MediaStreamError was emitted while closing the receiver.

The next useful check is a short stationary recording at measured hallway camera positions, then inspection of map/corner consistency in the failed multi-tag frames. More repetitions of the same spare-tag pose alone will not resolve this result.

Key files: `initial-pinhole-candidate.json`, `held-out-results.json`, `fit-stability.json`, and `capture-20260909T043401Z/hallway-diagnostic.json`, `hallway-geometry-gates.json`, `hallway-poses.jsonl`. Portable analysis scripts and metadata are included beside this report; see README.md for reproduction. The full recordings and local metadata ZIP are not committed.
