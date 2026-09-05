# perception

Capability area: Interaction. Milestone: M3.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the detection-event shape name one change owner and require cross-review.

Samples frames at 5 to 10 fps per stream from MediaMTX, runs a small detector (YOLO-class, people and common objects; thermal if mounted), and emits detection events with a world-position estimate from drone pose and camera geometry. Detections go to the relay as events, never as commands. Confidence >= 0.6 is shown, >= 0.8 is auto-promoted to focus, nothing is auto-acted on.

PRD: sections 4.8, 5.7.

The bounded COCO detector and its event payloads are documented in
[OBJECT_DETECTION.md](OBJECT_DETECTION.md).
