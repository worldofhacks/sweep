# Detection attention integration audit

Recorded-frame input reaches the existing `LiveDetectionWorker`, then follows the relay's audited event path to the Live focus view. The operator acknowledgement returns through the same authenticated console connection and produces only an acknowledgement event. It does not create an intent, call an intent sink, or deliver a node command.

| Boundary | Call | Result |
| --- | --- | --- |
| Recorded input | `POST /api/sessions/{session}/detections/recorded-frame` calls the configured `RecordedFrameProcessor` | The processor returns events emitted by `LiveDetectionWorker`. |
| Detection bridge | `RelayRuntime.record_detection_events` calls `DetectionAttention.record` | Frame outcomes and sightings are appended through `RelaySession.record_operator_events`, then published to subscribers. |
| Console | `WebSocketRelayClient` parses detection events and the control reducer stores them | A promoted sighting changes only `selectedFeedId`; the Live focus view renders an explicit acknowledgement button. |
| Operator acknowledgement | `WebSocketRelayClient.acknowledgeDetection` sends `detection_acknowledgement` | The relay accepts this frame only from the console principal, appends an audited acknowledgement event, and publishes it. |

`DetectionAttention` has no reference to `IntentV1`, `intent_sink`, command delivery, or adapter control. The recorded-frame integration test asserts an empty intent-sink call list and an audit log containing frame, detection, duplicate-suppression, and acknowledgement events.
