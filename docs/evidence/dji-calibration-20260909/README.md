# DJI calibration evidence — 2026-09-09

Diagnostic results from 20 fitting holds, three held-out views and a five-minute
handheld hallway recording with motors off. Start with
[the report](calibration-hallway-report.md).

**Owner-approved for the POC.** The owner qualified this calibration for the POC;
see [the approval record](poc-approval.json). The automated candidate retains its
formal rejection for missing independent FOV bounds. The hallway check passes joint geometry in only
5 of 19 multi-tag frames; no absolute camera-position ground truth was measured.

## Contents

- Pinhole parameters, pipeline description, frozen fitting selections, held-out
  results and leave-one-hold-out stability results.
- Original PNG bytes for all 20 fitting and three validation views, with SHA-256
  hashes. Other images referenced by the rejected-hold audit were not committed.
- Hallway sampled corner observations, raw pose disagreements and separate results
  from the existing geometry/ambiguity checks, including failed observations.
- Capture manifests and hashes identifying the original local H264 recordings.
  Full recordings, unselected PNGs and encoded-frame timing indexes remain in local
  `builds/dji-calibration-20260909` storage; they are not included in this Git package.
- `publication-manifest.json` records original metadata hashes and normalization.
  Metadata paths are package-relative; phone identifiers and connection details
  are redacted. Image bytes and numeric results are preserved.

## Reproduce

From the repository root, use an isolated Python environment with the versions
used for the offline checks:

```sh
python3 -m venv /tmp/dji-calibration-replay
/tmp/dji-calibration-replay/bin/pip install -r docs/evidence/dji-calibration-20260909/requirements.txt
/tmp/dji-calibration-replay/bin/python docs/evidence/dji-calibration-20260909/fit_initial.py
/tmp/dji-calibration-replay/bin/python docs/evidence/dji-calibration-20260909/validate_capture.py
/tmp/dji-calibration-replay/bin/python docs/evidence/dji-calibration-20260909/analyze_hallway.py
/tmp/dji-calibration-replay/bin/python docs/evidence/dji-calibration-20260909/check_hallway_geometry.py
```

These scripts overwrite their result JSON files. Use a disposable checkout to
preserve the committed snapshot. Floating-point results can vary slightly between
environments; the replay reproduced the reported metrics and all geometry counts.
Fitting/validation scripts verify included image hashes before using their corners.
Hallway scripts use the saved detections and repository map; they do not redetect
from omitted images, construct a qualified localizer or connect to hardware.

The map is `deployments/real-navigation/tag-map-53.json`, SHA-256
`7f4f1f2bf3e2ba0285568a7a0a78bd25587fcba82b5b31a97b4c592b00ca2009`.
Tags 35 and 49 are excluded. A different map or localization implementation can
change the hallway results. The pinned map retains its provisional provenance.

Expected results: fitting RMS about 0.152 px; held-out RMS about 0.205 px;
maximum focal change below 0.8%; 60 geometry-passing tag-pair comparisons with
median disagreement about 0.0795 m; joint geometry counts 5 pass / 14 fail.
