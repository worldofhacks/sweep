# Phone and media review

Reviewed the eight published heads in `phone_media.json` against their recorded bases, plus the unpublished verified-localization head `e45ebacbbd544ce6f7cc65ed6ca0f00fb399844b` against `2a66e6d97af477fe3770e925e86e7d043b00d30c`. The review followed data from hardware callbacks through raw records, parsing, signing, node admission, executor calls, and UI or storage side effects.

The published phone changes have no new patch from this pass. Their contracts either preserve diagnostic status for raw Android data or fail closed at the execution boundary. The camera path has a real probe implementation behind the camera port, and the route path reaches the flight executor only after signed admission and setup matching.

The verified-localization branch did have an unsafe evidence path. Commits `67ae7df081a5a9f042bafd8f255f952140b32678` and `4d1676775586cdae188e7450f776746d3347e1aa` in `/var/tmp/gauntlet/sweep-audit-phone-84` bind artifacts to configured values, bind the raw run and Android boot, reject excessive telemetry timing residuals, label replay fixtures as test doubles, decode the exact hashed frame bytes, and send live input through the publisher's bounded periodic loop. The public `capture_aligned_attitude` schema accepts an external measured adapter only when its calibrated convention, run identity, boot ID, interpolation bracket, residual, uncertainty, and evaluated capture time match the pinned contract. Raw `KeyGimbalAttitude` values remain diagnostic `raw_sdk_axes` data and are never converted with generic RPY.

No Android producer currently supplies decoded frames or the capture-aligned attitude adapter. The CLI can consume that external data through its explicit contract, while physical calibration remains pending. Android velocity and height continue through the real publisher/fuser path and cannot produce a ready pose by themselves.

## Published PR findings

| PR | Head | Result | Hardware or demo call chain checked |
| --- | --- | --- | --- |
| 177 | `b42bb0960afc556342d6a110adefd1978afca1ce` | Pass | Relay command → `CameraExecutor` → `CameraPort` → `DjiCameraPort`; shutter evidence and immutable media publication are fenced by connection generation. Fake camera remains a test implementation. |
| 180 | `2d8343a36089de0f88fece61b4159a794194bb9e` | Pass | Signed wire JSON → `NavigationRouteAuthorization` and `NavigationPose` parsing → signature/provenance fields consumed by admission. |
| 185 | `d84d306a0c1d9247e2fddeb93ddfa855d4fbc7da` | Pass | `RelayLink` receives signed authorization and pose → checks identity, route, freshness, and authority → only admits matching `GOTO`. |
| 186 | `d18c51e8b37d5b37d577cf052c162757a73c26ca` | Pass | Admitted route → `FlightExecutor` → `FlightController.navigationCheck` → bounded Virtual Stick frames, HOLD, then LAND after loss. |
| 190 | `1b2348a6a9b2ed524ff4e544c9ba31a37433d915` | Pass | Signed setup import → `BridgeSetupStore`/`BridgeNode` → matching navigation config in `RelayLink` and `FlightExecutor`; mismatches leave navigation disabled. |
| 227 | `0d3cca17b825e1a5a6b98f53beaa66b6c8cea450` | Pass with explicit limitation | MSDK listeners → `SensorRawSink` queue → JSONL and ZIP manifest export. Attitudes retain receipt timing and raw gimbal axes; they are evidence only, not control-localization input. |
| 228 | `2a66e6d97af477fe3770e925e86e7d043b00d30c` | Pass | Android JSONL → `SensorRecordAdapter` → publisher shape checks. It deliberately emits unverified velocity and height, so the fuser refuses ready localization. |
| 229 | `adbaf440e5fe0ad65ca0148e6a4ba5837348192c` | Pass | MediaMTX recording compose overlay → completed segments → archive-relative SHA-256 manifest. The opt-in profile avoids changing normal stream behavior. |

## Verification

The #84 patch passed `pytest -q perception/test_verified_localization.py perception/test_sensor_records.py perception/test_control_publisher.py` with 56 tests, Ruff checks and format checks, and `git diff --check`. Pytest printed pre-existing cleanup warnings for unrelated MediaMTX recording directories; the test results were successful.

The published branches were reviewed from the exact saved diffs at `pr-177.diff`, `pr-180.diff`, `pr-185.diff`, `pr-186.diff`, `pr-190.diff`, `pr-227.diff`, `pr-228.diff`, and `pr-229.diff` in this directory. No simulated run was represented as physical flight evidence.

## Revised recording head

The later #229 revision `19c7132` added the bounded recording helper, durable export verification, and a pinned image digest. Review followed every helper function from CLI parsing through service startup, storage polling, finalization, hashing, and export.

Different working roots could acquire independent locks while controlling the same MediaMTX container. Commits `cccc7f2` and `4614cd0` serialize the service lifecycle with one global lock and refuse a running service before creating a run or executing cleanup. The cross-process regression uses an isolated test lock. The final source head is `4614cd0821ffb8038f4cb4515a67a6014ce3ac27`; all 17 recording tests passed with real Docker, FFmpeg, and ffprobe.
