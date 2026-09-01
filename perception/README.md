# perception

Owner: A (Interaction and perception). Phase 3.

Samples frames at 5 to 10 fps per stream from MediaMTX, runs a small detector (YOLO-class, people and common objects; thermal if mounted), and emits detection events with a world-position estimate from drone pose and camera geometry. Detections go to the relay as events, never as commands. Confidence >= 0.6 is shown, >= 0.8 is auto-promoted to focus, nothing is auto-acted on.

PRD: sections 4.8, 5.7.
