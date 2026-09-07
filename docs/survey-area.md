# Pilot-assisted survey area

A console submits confirmed `survey_area {"area_id": "…"}` with exactly one selected ready ground node. The relay records its current accepted pose identity, then accepts canonical `range_scan` observations for that node and epoch. Surveying never issues a drive command.

The console finishes or cancels the active run on its authenticated session WebSocket with this bounded frame:

```json
{
  "v": 1,
  "t": 1756700000000,
  "type": "survey_lifecycle",
  "event_id": "console-event-42",
  "session": "demo-1",
  "operation": "complete",
  "intent_id": "survey-42",
  "run_id": "survey-survey-42",
  "connection_epoch": 3
}
```

`operation` is `complete` or `cancel`. The request must identify the active intent, run, and connection epoch exactly. Its event ID passes the normal relay transport replay gate. The relay refuses a stale epoch, an unknown run, malformed input, or a completion without a scan. It fails an active run when the ground adapter disconnects, the pose is no longer current, the duration expires, or its evidence reaches a hard bound.

Completion writes one create-only JSON candidate under the relay audit root’s `survey_candidates` directory. The candidate includes the selected node and epoch, the accepted pose identity, and each admitted scan with its source, event, frame, timestamp, and sensor-pose identity. A write either publishes the entire candidate or leaves no candidate file. Candidate output is evidence for later mapping approval; it does not authorize movement or publish a map.
