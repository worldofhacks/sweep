# Localization software measurement evidence

The command **python -m evals.flight_acceptance** calculates bounded localization
measurements for exactly five recordings. It does not approve a flight, authenticate
evidence, exercise failure drills, or establish release readiness. A synthetic fixture
can test the evaluator and receive a software pass; it cannot count as one of the
physical rehearsals required by issues #86 and #145.

The physical gate remains external. Its signed record must show five complete
launch-to-lobby-to-kitchen-hold-return-land rehearsals on the approved route, the
command and JSONL audit streams, pose traces and video, reference-instrument records,
the covered-tag, wrong-map and link-silence drills, RC intervention evidence, and the
current map/route approval. Issue #82 governs the map and route evidence; issue #84
governs capture-time localization and calibrated latency.

## Two hash-bound inputs

The command requires:

1. An externally reviewed evaluation manifest. It pins the approved route identifier,
   route digest and ordered checkpoint geometry; aircraft, map, geometry, camera,
   body-extrinsics and latency-calibration digests; localizer build/configuration
   digests; reference and clock-alignment calibration; the exact five immutable raw
   recording-bundle digests; and measurement bounds.
2. One evidence document containing the five normalized measurement runs. Its
   **evaluation_manifest_sha256** must equal the SHA-256 of the exact manifest file
   bytes.

The evaluator compares every run with the manifest and includes SHA-256 digests of
both input files in the report. A digest proves byte identity, not who captured,
reviewed, or approved the bytes. Preserve the signed manifest and the five raw
recording bundles outside this report. **raw_run_evidence_sha256** means the digest of
one immutable raw bundle, not a digest invented from the normalized samples.

The manifest has this strict shape; all shown digest values must be 64 lowercase hex
characters:

~~~json
{
  "schema_version": 1,
  "manifest_kind": "localization_software_measurement_manifest",
  "manifest_id": "owner-reviewed-route-evaluation-v1",
  "route": {
    "route_id": "approved-lobby-kitchen-return-v1",
    "route_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "minimum_duration_s": 60.0,
    "maximum_duration_s": 600.0,
    "minimum_hold_duration_s": 12.0,
    "checkpoints": [
      {
        "checkpoint_id": "launch",
        "phase": "launch",
        "position_map_m": [0.0, 0.0, 1.2],
        "radius_m": 0.1
      },
      {
        "checkpoint_id": "lobby-outbound",
        "phase": "lobby",
        "position_map_m": [2.0, 0.0, 1.2],
        "radius_m": 0.1
      },
      {
        "checkpoint_id": "corridor-outbound",
        "phase": "corridor",
        "position_map_m": [6.0, 0.0, 1.2],
        "radius_m": 0.1
      },
      {
        "checkpoint_id": "kitchen-hold-start",
        "phase": "kitchen_hold_start",
        "position_map_m": [12.0, 0.0, 1.2],
        "radius_m": 0.1
      },
      {
        "checkpoint_id": "kitchen-hold-complete",
        "phase": "kitchen_hold_complete",
        "position_map_m": [12.0, 0.0, 1.2],
        "radius_m": 0.1
      },
      {
        "checkpoint_id": "lobby-return",
        "phase": "return",
        "position_map_m": [2.0, 0.0, 1.2],
        "radius_m": 0.1
      },
      {
        "checkpoint_id": "land",
        "phase": "land",
        "position_map_m": [0.0, 0.0, 1.2],
        "radius_m": 0.1
      }
    ]
  },
  "deployment": {
    "aircraft_id": "mini3-serial-1",
    "map_bundle_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "geometry_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "camera_calibration_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "body_extrinsics_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "latency_calibration_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  },
  "estimator": {
    "source_id": "fused-localizer",
    "build_sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "config_sha256": "1111111111111111111111111111111111111111111111111111111111111111"
  },
  "reference": {
    "source_id": "survey-total-station",
    "method": "surveyed total station",
    "calibration_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
    "clock_alignment_sha256": "3333333333333333333333333333333333333333333333333333333333333333",
    "maximum_calibration_bound_m": 0.02,
    "maximum_clock_alignment_bound_s": 0.01
  },
  "expected_raw_run_evidence_sha256": [
    "4444444444444444444444444444444444444444444444444444444444444444",
    "5555555555555555555555555555555555555555555555555555555555555555",
    "6666666666666666666666666666666666666666666666666666666666666666",
    "7777777777777777777777777777777777777777777777777777777777777777",
    "8888888888888888888888888888888888888888888888888888888888888888"
  ],
  "limits": {
    "minimum_estimate_samples_per_run": 119,
    "minimum_reference_samples_per_run": 119,
    "minimum_localization_updates_per_run": 119,
    "max_pairing_age_s": 0.05
  }
}
~~~

The non-transit checkpoint phases must be, in order: **launch**, **lobby**,
**corridor**, **kitchen_hold_start**, **kitchen_hold_complete**, **return**, and
**land**. Additional reviewed checkpoints use phase **transit** and may appear between
those phases. Launch is first, land is last and returns to the launch zone, and both
kitchen-hold checkpoints name the same volume. The ordered path must span at least
1 m. Its minimum duration cannot be shorter than path length at the fixed 0.5 m/s
route-tube speed plus **minimum_hold_duration_s**. The three sample minimums cannot be
lower than the number needed to cover that duration with no interval over 500 ms.

Each evidence run has these exact fields:

- **run_id**
- **manifest**, containing **raw_run_evidence_sha256**, the exact route/deployment/
  estimator pins, **session_id**, **clock_id**, and the independently sourced
  reference calibration and clock-alignment bounds
- **interval** with strictly ordered **start_s** and **end_s**; the launch and land
  crossings must match those boundaries
- **estimates** and **references**
- **localization_updates**
- **checkpoint_crossings**, mapping the manifest's exact ordered checkpoint IDs to
  distinct reference sample IDs

Available position samples contain **id**, **timestamp_s**, **source_id**,
**clock_id**, **status: available**, and **position_map_m**. An unavailable or invalid
position sample replaces **position_map_m** with a nonempty **reason**. Available
localization updates omit both position and reason. Unknown fields, duplicate
keys/IDs, out-of-order or out-of-interval timestamps, source/clock mismatches,
booleans in numeric fields, nonfinite/extreme numbers, and overlapping runs in one
session clock are refused.

## Measurement

References are paired in timestamp order to the earliest still-feasible unused
estimate. This linear, maximum-cardinality matching cannot consume a later estimate
that a later reference needs. Pairing never interpolates or extrapolates.

For each pair, the conservative position-error bound is:

~~~text
measured Euclidean distance
+ reference calibration bound
+ 0.5 m/s × (pairing age + clock-alignment bound)
~~~

Every run and the aggregate must have nearest-rank p95 at or below 0.25 m. Every
recorded status must be available, each reference must pair, and each pinned
checkpoint must be crossed in order. The reference calibration bound is included
when checking a checkpoint radius. Reference samples throughout the pinned hold
interval must remain in its volume for at least **minimum_hold_duration_s**.
Checkpoint timing uses the independently measured crossing positions and their
calibration bounds to reject a claimed segment whose average travel would exceed
0.5 m/s; the independently retained command stream remains the authority for the
actual speed-cap drill. Sample minimums and duration bounds must pass, and neither
available localization updates nor paired references may leave a
run-boundary-inclusive gap over 500 ms.

## Resource and path limits

- Each input file: regular UTF-8 JSON, at most 16 MiB.
- JSON: at most 32 nested containers, 300,000 values, and 2,048 characters per
  string.
- Each run series: at most 10,000 records; route: at most 64 checkpoints; run:
  at most 3,600 seconds.
- Serialized report: at most 32 MiB.
- Input/output paths cannot contain symbolic-link components. The output parent must
  already exist, and the output itself must be new. Publication uses a flushed
  temporary file and an atomic no-overwrite hard link.

## Run it

~~~bash
uv run python -m evals.flight_acceptance \
  rehearsals.json \
  --evaluation-manifest evaluation-manifest.json \
  --output localization-software-report.json
~~~

A software pass exits 0. Valid measurements that miss a criterion still write their
report and exit 1. Malformed, mismatched, oversized, or unsafe-path inputs write no
report, emit one **localization software evidence refused:** diagnostic, and exit 1.
Without **--output**, the bounded report is written to standard output.
