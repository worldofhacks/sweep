# Flight acceptance software evidence

`python -m evals.flight_acceptance` checks five recorded mapped-route rehearsals against independently measured reference positions. A passing report requires exactly five distinct recording runs, every run and the aggregate at or below 0.25 m nearest-rank p95 position error, and no localization-update interval longer than 500 ms. Flight authorization remains an external decision based on the supplied recordings and separate acceptance records.

Each run pins its map, geometry, aircraft, camera calibration, body extrinsics, and common clock identity. Reference evidence names its source, measurement method, calibration identity, and calibration bound. The evaluator rejects a reference source ID that is the same as the estimator source ID, but the declared independence and reference quality remain claims supported by the recorded measurement process.

Record physical failure and RC drills as separate signed acceptance measurements. Keep their evidence with the flight-operation record; it is outside this software report.

## Evidence file

The input is one JSON document. Values shown here are labels, so replace each with the exact identity used during the rehearsal.

```json
{
  "schema_version": 1,
  "criteria": {"max_pairing_age_s": 0.1},
  "runs": [
    {
      "run_id": "rehearsal-01",
      "route_id": "kitchen-to-lobby",
      "manifest": {
        "map_id": "map-content-sha256",
        "geometry_id": "geometry-content-sha256",
        "aircraft_id": "aircraft-serial",
        "camera_calibration_id": "camera-calibration-sha256",
        "body_extrinsics_id": "body-extrinsics-sha256",
        "clock_id": "room-monotonic-clock",
        "session_id": "rehearsal-session-2026-09-06-01",
        "estimator_source_id": "fused-tag-localizer",
        "reference": {
          "source_id": "total-station-serial",
          "method": "surveyed total station",
          "calibration_id": "total-station-calibration-record",
          "calibration_bound_m": 0.01,
          "independence_claimed": true
        }
      },
      "interval": {"start_s": 0.0, "end_s": 0.5},
      "estimates": [
        {
          "id": "estimate-0001",
          "timestamp_s": 0.1,
          "clock_id": "room-monotonic-clock",
          "source_id": "fused-tag-localizer",
          "status": "available",
          "position_map_m": [1.2, 0.4, 1.1]
        }
      ],
      "references": [
        {
          "id": "reference-0001",
          "timestamp_s": 0.13,
          "clock_id": "room-monotonic-clock",
          "source_id": "total-station-serial",
          "status": "available",
          "position_map_m": [1.19, 0.41, 1.1]
        }
      ],
      "localization_updates": [
        {
          "id": "update-0001",
          "timestamp_s": 0.0,
          "clock_id": "room-monotonic-clock",
          "source_id": "fused-tag-localizer",
          "status": "available"
        },
        {
          "id": "update-0002",
          "timestamp_s": 0.5,
          "clock_id": "room-monotonic-clock",
          "source_id": "fused-tag-localizer",
          "status": "available"
        }
      ]
    }
  ]
}
```

Supply five runs, each with a unique `run_id`. Repeated `route_id` values record complete rehearsals of the approved route. A `session_id` can cover more than one rehearsal. Map, geometry, aircraft, camera calibration, and body extrinsics identities must be identical across all five runs. A run can use a different clock identity, provided every estimate, reference, and localization update in that run uses its listed clock. Timestamps are seconds in that clock and are strictly increasing within each series. The interval includes the start and end of the recorded rehearsal. Available localization updates and successfully paired reference samples must each cover every 500 ms interval, including both boundaries.

Use `status: "unavailable"` or `"invalid"` with a nonempty `reason` when a recorded sample lacks a usable value. Omit `position_map_m` for either status. The report retains the counts and fails the software checks. Missing fields, duplicate IDs, nonfinite values, out-of-order timestamps, a wrong clock, or a source mismatch are malformed evidence and produce no report.

## Run the evaluator

```bash
uv run python -m evals.flight_acceptance rehearsals.json --output flight-software-report.json
```

The output path must be new. The command writes a report for valid evidence even when the criteria fail, then exits with status 1. Malformed evidence also exits with status 1 and names the rejected condition. With no `--output`, the report is written to standard output.

For each available reference, the evaluator selects the nearest unused available estimate within `max_pairing_age_s`, which must be greater than zero and at most 0.5 seconds. It reports the direct Euclidean distance in the declared map frame. It never interpolates or extrapolates an estimate. The p50 and p95 are nearest-rank values from the paired errors, and p95 uses rank `ceil(0.95 × count)`.

The report contains the SHA-256 of the input evidence file, a SHA-256 of the report body, every run manifest, matched samples, status counts, error distributions, errors above 0.25 m, and update-gap segments above 500 ms. It also records paired-reference coverage gaps. The calculation uses recorded positions and timestamps. It omits estimator confidence, covariance, and unrecorded accuracy assumptions.
