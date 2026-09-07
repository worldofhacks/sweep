# Pilot-assisted survey area

A confirmed `survey_area {"area_id": "…"}` records canonical lidar evidence from one selected ready ground node. It does not issue a drive command. The run pins the node epoch, accepted pose identity, configured lidar source, odometry frame, lidar frame, and configured mount ID. A change to any pinned input fails the run.

Completion publishes one immutable directory under the audit root's `survey_candidates` directory. It contains the canonical recording, a local occupancy PNG and metadata, the sensor pose path, a tag-candidates document, and `candidate.json`. The recording and grid use the `ohmni_scan_record` and `ohmni_occupancy_grid` artifact formats. Reload checks each named artifact through bounded no-follow reads. The candidate is local odometry evidence and has no movement authority.

The console completes or cancels a run on its authenticated session WebSocket:

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

`operation` is `complete` or `cancel`. The request identifies the current intent, run, and epoch. Its event ID uses the normal relay replay gate. Cancellation remains available after a timeout, stale pose, stale source, or readiness loss. Completion checks those conditions again and requires at least one current scan.

A candidate ID is a SHA-256-derived identity of the session, intent, and run. The artifact directory is created in a temporary sibling directory and published by rename. If the audit operation fails, the lifecycle restores the run and removes the published candidate.
