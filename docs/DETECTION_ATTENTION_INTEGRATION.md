# Detection attention integration audit

Recorded-frame detection is enabled with `SWEEP_DETECTION_MODEL_PATH` and
`SWEEP_DETECTION_RECORDINGS_JSON`. Startup verifies the pinned YOLOX model and each
configured image digest, then builds `HostRecordedFrameProcessor`. A recording binds one
image to an aircraft, camera source, and mission. Processing accepts only an active
aircraft connection and uses its connection epoch in the worker identity.

| Boundary | Call | Result |
| --- | --- | --- |
| Recorded input | `POST /api/sessions/{session}/detections/recorded-frame` calls the configured processor | The processor returns events emitted by `LiveDetectionWorker`. The request body is limited to 4 KiB. |
| Detection bridge | `RelayRuntime.record_detection_events` calls `DetectionAttention.record` | Frame outcomes and sightings are appended through `RelaySession.record_operator_events`, then published to subscribers. A failed audit does not change acknowledgement state. |
| Console | `WebSocketRelayClient` parses detection events and the control reducer stores them | A promoted sighting focuses its source locally and marks its Wall tile for review. Fleet selection does not change. |
| Operator acknowledgement | `WebSocketRelayClient.acknowledgeDetection` sends `detection_acknowledgement` | The relay accepts this frame only from the console principal, appends an audited acknowledgement event, and publishes it. |

Duplicate observations retain the first detection ID and acknowledgement state. The bridge keeps at most 128 detections per session. An acknowledgement is refused after its aircraft reconnects.

The acceptance image is COCO 2017 validation image `000000397133`, published by the
[COCO dataset](https://images.cocodataset.org/val2017/000000397133.jpg). On 2026-09-06,
the pinned `yolox_s.onnx` model (`c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063`)
detected one `person` at 0.897 confidence from the cached image digest
`09e1d25c75f7879bdaa69c327fece5cabacd53939c8c2ef9e87f1c97a2e478c4`. The image and
model remain outside the repository.
