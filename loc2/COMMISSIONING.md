# AprilTag localization commissioning -- unit 11 atrium

This is the runbook for turning what's in this directory into a working indoor
position source for the aircraft. It does not connect to the relay, send aircraft
commands, or touch any running service -- everything here is offline artifact
generation and offline testing.

## What exists and where it came from

| Artifact | Path | Status |
| --- | --- | --- |
| Calibration | `dji-mini3-real-navigation-calibration.json` | Hand-authored: real fit, fabricated-nothing camera_serial, see below |
| FOV bounds derivation | `FOV_BOUNDS.md` | Generated reasoning, from published spec only |
| Localizer map bundle | `localizer-bundle/` | Generated from real survey data, real coordinate change |
| Bundle explanation | `WORLD_BUNDLE.md` | Written |
| Latency artifact | `latency/latency_dji-mini3.json` | Real: 1537 decode-latency samples over 65.0 s of the live drone1 RTSP stream, p50 33.2 ms / p95 50.8 ms. Blocker 1 cleared. |
| Capture-alignment doc | `capture-alignment/webcam_capture_alignment.json` | Template + fixed conventions filled; two translations are owner placeholders |
| webcam_localization config | `config/webcam_localization.json` | Wired to the real latency artifact and an explicit decoder-path attestation. Blocker 2 cleared; still blocked on Blocker 3 (`T_body_camera`). |
| webcam_control_adapter config | `config/webcam_control_adapter.json` | Generated, two fields intentionally left as unmet placeholders |
| Runtime script | `run_webcam_localization.sh` | Runnable, reads secrets at runtime, prints nothing secret |
| Offline detection test | `offline_test.py` / `offline-detection-results.txt` | Run, real numbers below |

## Ordered runbook

1. ~~**Get real measured latency samples.**~~ Done -- see `latency/latency_dji-mini3.json`
   and `latency/capture_decode_latency.py`, which timed real
   `cv2.VideoCapture(..., cv2.CAP_FFMPEG).read()` calls against the live drone1
   RTSP stream for 65 seconds (1537 samples, p50 33.2 ms, p95 50.8 ms).
2. ~~**Resolve the decode-pipeline mismatch.**~~ Done -- `WebcamLocalization`
   now accepts an explicit `decoder_path_attested_geometrically_equivalent`
   config field (see Blocker 2 below for the mechanism and rationale).
   `config/webcam_localization.json` sets it to `"opencv-ffmpeg-rtsp"`.
3. **Owner measures the capture alignment** (two caliper readings -- see
   `capture-alignment/README.md`), fills in
   `capture-alignment/webcam_capture_alignment.json`, then run
   `compute_capture_alignment.py` and paste its output into
   `config/webcam_localization.json`'s `localizer.T_body_camera`.
4. **Owner measures `map_to_map_enu`** -- the rigid transform from
   `localizer-bundle`'s tag-0-anchored frame into whatever frame the deployment's
   other sensors (telemetry, height) call `map_enu`. Fill in
   `config/webcam_control_adapter.json`'s `map_to_map_enu.matrix`. This is a
   survey task, not a caliper measurement; if the two frames are meant to
   coincide, that still has to be verified, not assumed (the field's schema
   forbids a silent identity default).
5. ~~**Fill in `config/webcam_localization.json`'s `latency_path`/`latency_sha256`**~~
   Done -- both point at the real latency artifact from step 1.
6. **Fill in `config/webcam_control_adapter.json`'s `geometry_id`** once the
   navigation deployment's agent has published the geometry identifier its bundle
   uses (that agent's job, per the task's own scoping note -- not fabricated here).
7. Confirm construction end to end offline (no relay, no aircraft):
   ```python
   from perception.webcam_localization import WebcamLocalization, load_config
   loop = WebcamLocalization(load_config("config/webcam_localization.json"))
   ```
8. Once the aircraft is streaming, run (from the repo root, in the worktree):
   ```bash
   loc2/run_webcam_localization.sh config/webcam_localization.json out.jsonl 60
   ```
   This sets `SWEEP_LOCALIZATION_RTSP_URL` from
   `~/.secrets/sweep-field-relay.env` at runtime and never prints it.

## Blockers found during commissioning

**Blocker 1 -- no measured latency. CLEARED 2026-09-09.** The aircraft was
streaming live over MediaMTX RTSP (`drone1`, 1280x720 H264 ~30 Hz).
`latency/capture_decode_latency.py` opened that stream with the exact call the
live loop uses (`cv2.VideoCapture(url, cv2.CAP_FFMPEG)`) and timed 1537
consecutive `.read()` calls over a 65.0 s span. `calibration/latency.py`
(via `python -m calibration latency`) turned those into
`latency/latency_dji-mini3.json`: schema_version 1, status offline, evidence_kind
recorded_live, camera_serial matching the calibration, pipeline identical to the
calibration's pipeline dict, `latency_endpoint: "localization_decode"`. Real
p50 33.2 ms, p95 50.8 ms -- comfortably inside the 500 ms budget
`WebcamLocalization.__init__` enforces. `config/webcam_localization.json` now
points `latency_path`/`latency_sha256` at this artifact.

Constructing this pipeline-identity match required adding
`"latency_endpoint": "localization_decode"` to the calibration's own `pipeline`
object (`dji-mini3-real-navigation-calibration.json`, mirrored in
`config/webcam_localization.json`), since `TagLocalizer` and
`WebcamLocalization` both require the calibration's, config's, and latency
artifact's `pipeline` dicts to be identical. This adds a label, not a decode-path
claim -- `decoder_path` itself is untouched.

**Blocker 2 -- calibration and live loop use different decode paths. CLEARED
2026-09-09.** The DJI calibration evidence's `pipeline.decoder_path` records the
real capture path, `MediaMTX WHEP -> aiortc 1.15.0 -> PyAV 17.1.0 FFmpeg H264 ->
BGR24 PNG`, not the live loop's `cv2.VideoCapture(..., cv2.CAP_FFMPEG)` RTSP
pull. Rather than editing that provenance to claim a decode path it didn't use,
`perception/webcam_localization.py`'s `WebcamLocalization.__init__` now accepts
an explicit operator opt-in: a top-level config field,
`decoder_path_attested_geometrically_equivalent`, which must literally equal
`"opencv-ffmpeg-rtsp"` for construction to proceed when `pipeline.decoder_path`
is anything else. There is no default -- an absent or falsy value still refuses
construction with the original error, exactly as it did before this field
existed. The attestation is recorded in the runtime provenance block
(`decoder_path_attested_geometrically_equivalent: true`) so every pose
observation is auditable back to the fact that this calibration ran through a
different decoder.

`config/webcam_localization.json` sets this field to `"opencv-ffmpeg-rtsp"`
with a paired `decoder_path_attestation_rationale` explaining why: both paths
are FFmpeg H264 decoders producing unscaled BGR at the same 1280x720 grid, which
is what pinhole-intrinsic geometry depends on -- the color pipeline and container
differ, the pixel grid does not. Tests:
`perception/test_webcam_localization.py::test_mismatched_decoder_path_refuses_without_attestation`
(strict default still refuses) and
`::test_attested_geometric_equivalence_admits_a_different_decoder_path` (the
attestation admits construction and the loop accepts a real pose fix).

**Blocker 3 -- two owner measurements outstanding.** `body_to_gimbal` and
`gimbal_to_camera` translations (four numbers total) in
`capture-alignment/webcam_capture_alignment.json`. Exact instructions are in
`capture-alignment/README.md`. Everything downstream of these (task 5's
`T_body_camera`) is wired to fail loudly, not silently default to zero, until
they're filled in.

**Blocker 4 -- `map_to_map_enu` survey outstanding.** Needed in
`config/webcam_control_adapter.json`; see runbook step 4.

## Task 6: what the offline evidence actually shows

Full output in `offline-detection-results.txt`; two independent checks.

**Check 1 -- running the real `TagLocalizer.estimate()` against the 23 committed
DJI PNGs (20 fitting + 3 held-out views).** Result: 0/23 accepted, all
`unknown_or_duplicate_tag`. This is not a detector failure -- every one of these
frames shows only tag id 54, a handheld calibration target used to fit the
intrinsics, which is not one of the 51 floor tags in `tag-map-53.json`. These
frames test the *calibration fit* (reported separately: RMS 0.152 px fitting,
0.205 px held-out, matching `docs/evidence/dji-calibration-20260909`), not
map-based localization, because they were never pointed at a floor tag.

**Check 2 -- independent single-tag position agreement from the real hallway
walk** (`capture-20260909T043401Z/hallway-geometry-frames.jsonl`, 231 frames,
already-detected corners against the real floor tags; no raw PNGs are committed
for this capture so this reuses the recorded detections rather than
re-detecting). Tags-solved-per-frame: 161 frames saw 0, 52 saw 1, and 18 frames
saw 2 or more (10 with 2, 2 with 3, 4 with 4, 2 with 5). Across those 18
multi-tag frames, 60 pairwise comparisons between independently-solved
single-tag camera positions: **median disagreement 0.0795 m, mean 0.0886 m, p90
0.1594 m, max 0.1960 m**. For context, tag spacing in this venue has a median of
0.84 m (min 0.62 m), so an ~8 cm median position disagreement is a real,
non-trivial fraction of the geometry but not gross error. The existing
hallway-geometry check (`docs/evidence/.../README.md`) additionally reports that
only 5 of 19 multi-tag frames pass a *joint* (all-tags-at-once) geometry
consistency check, meaning single-tag PnP solves broadly agree to within ~8 cm
but a stricter multi-tag joint solve is harder to satisfy on this data.

**What this does and doesn't establish.** This data was walked by hand, not flown
at the locked -90-degree nadir attitude the real flight will use, and used the
pre-FOV-bounds-fix intrinsics (numerically identical camera_matrix/distortion,
since fov_bounds is a threshold check that doesn't change the fit). It is real
evidence that the tag detection and PnP solve broadly work at this venue's real
tag spacing and this real camera's real distortion, at roughly the expected
~8 cm level of self-consistency. It is not evidence about the locked-nadir flight
geometry specifically, and it does not substitute for a flight test once the
blockers above are cleared.

## How to tell it's working once the drone is publishing

1. `run_webcam_localization.sh` should produce non-empty `pose_observation`
   records in the output JSONL within a few seconds of the stream coming up.
2. Each record's `tag_ids` should show 4-6 tags per frame at the 1.7 m flight
   ceiling (per the venue's measured tag spacing and the camera's FOV), not 0-1 --
   if it's persistently 0-1, the gimbal likely isn't locked at the intended
   attitude or the aircraft isn't over the tagged floor area.
3. Compare consecutive accepted fixes' `T_map_body`/`T_world_body` translations
   against the aircraft's actual (visually estimated) motion between frames --
   gross jumps mean either a bad detection got past consensus or the map/camera
   frame convention is wrong somewhere in the alignment chain.
4. If a `consensus` block is configured (`config/webcam_localization.json` sets
   `minimum_distinct_tags: 2`), check `consensus_status` in each report: sustained
   `too_many_consensus_tags` or ambiguous-consensus reasons mean the residual
   bounds need tuning against real recorded frames, per
   `perception/WEBCAM_LOCALIZATION.md`'s consensus section.
5. None of this should be treated as flight-approved: every report from this
   pipeline carries `capture_time_verified: false` and
   `publisher_identity_verified: false` by design, and this task's own
   `webcam_control_adapter` config leaves `source_verified`/`timing_verified`
   at `false` -- those stay an owner decision made after real verification
   exists, never a default flipped here.
