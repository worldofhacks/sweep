# Bounded object-detection event producer

`YoloXOnnxDetector` implements YOLOX-s COCO preprocessing and output decoding through OpenCV
DNN. The declared default target set is `person`, `backpack`, `suitcase`, and `bottle`;
callers may provide another nonempty set of unique COCO labels. Every processed-frame event
records the canonical target-label list, and outputs outside that configured set fail closed.
The model binary stays outside the repository. One compatible Apache-2.0 reference artifact is
the official release at
<https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx> and
has SHA-256 `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063`.
For a file-backed detector, the constructor reads at most 128 MiB once, verifies the expected
digest, and gives those same bytes to OpenCV. Injected test nets instead require an explicit
synthetic model fingerprint. Every processed-frame and sighting payload carries
`detector_config_sha256`, an immutable digest over a versioned detector-schema identifier,
model digest or synthetic fingerprint, confidence and NMS thresholds, canonical target labels,
and candidate cap. The identifier does not fingerprint Python source or the OpenCV runtime, so
those versions must be recorded separately by a future durable sink. This binding prevents
silent model/config substitution inside the new event contract; it is not recorded-footage or
site-acceptance evidence.

`LiveDetectionWorker` accepts any `FrameReader` and can be paired with `WebcamStream`. It
reads one latest frame per poll, retains at most 4,096 events, and rejects stale, future,
or regressed frame times. Its aggregator retains at most 4,096 active sightings. A fresh frame
produces a `perception.frame_processed` outcome and
each detected COCO candidate produces `perception.sighting`. Within one source, mission, and
worker run, overlapping observations share a `sighting_id` and increment
`observation_count`; the candidate and frame identity in each emitted sighting always describe
the same current observation. `SightingAggregator` is only bounded, short-window IoU grouping;
it is not the BoT-SORT/camera-motion-compensated tracker selected in the prior-art decision.
That recorded-stack implementation and validation remain open in #96.

The frame time supplied by `WebcamStream` is the host's monotonic time immediately after
OpenCV returns a decoded frame. It is explicitly published as
`frame_decoded_at_monotonic_s` with `clock_domain=host_monotonic` and
`frame_time_provenance=decoder_completion`. It is not a camera capture time and cannot yet
align a detection with aircraft pose or recorded-video time. Events separately record
`evaluation_started_at_monotonic_s` and `evaluation_completed_at_monotonic_s`; the latter is
sampled only after inference and atomic sighting aggregation finish. Freshness is checked after
both stages, and stale aggregation is rolled back, so neither slow inference nor slow
aggregation can publish or retain an already-stale sighting. A randomly generated
`worker_run_id` prevents frame, event, and sighting identity reuse across worker instances; IDs
also include mission and source. The run ID spans any reconnects hidden inside its
`FrameReader`; it is not a decoder-connection or camera-capture identifier. A caller that
injects it for deterministic replay must provide a new globally unique value for each worker
run.

The worker forms each complete event batch before invoking its synchronous callback and keeps
a bounded diagnostic history; this is not durable logging. Aggregation failures still retain a
processed-frame event with an `aggregation_error` outcome. Callback, aggregation, or polling
failures stop background processing and are exposed through a specific `failure_reason`; an
external supervisor is still required. The callback emits no command or
motion request. This module is not instantiated by the relay or console in this change, so
durable logging, feed promotion within one second, operator confirmation, camera capture
timestamps, pose/world-position estimates, recorded-footage acceptance, and real aircraft
camera integration remain open parts of #62. Synthetic unit tests cover this library boundary;
they are not live-camera or site qualification.
