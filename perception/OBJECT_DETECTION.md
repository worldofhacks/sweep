# Live object detection

`YoloXOnnxDetector` runs the Apache-2.0 YOLOX-s COCO model through OpenCV DNN. The
runtime defaults to `backpack`, `bottle`, and `suitcase`; callers may select other COCO
labels. The model binary stays outside the repository. Download the official release
artifact at <https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx>
and verify SHA-256 `c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063` before use.

`LiveDetectionWorker` reads a single latest frame from `WebcamStream` on each poll. It
retains a fixed number of emitted events and drops frames older than its configured age.
Every consumed frame produces a `perception.frame_processed` event with source, frame,
mission, capture and processing times, including empty, stale, future, and detector-error
outcomes. A detected COCO candidate produces `perception.sighting`; overlapping candidates
in the same source and mission share a `sighting_id` and increase `observation_count`.

The worker accepts an event callback and emits no command or motion request. Relay transport,
operator attention, and confirmation consume the event payloads at their boundary.
