# Search PR audit

Reviewed pinned heads #183, #184, #195, #196, #198, #199, #200, #202, #203, #205, #208, #209, #210, #211, #212, and #220. The search path is sound for synchronous dispatch after the fixes below. Two production defects were found in the relay layer and fixed in commit `15a0cb1606d140f31d01f8dbc3cdd39dd441032d`.

The audit followed every changed production function through the path from `IntentV1` validation, search mission preparation, dispatcher execution and node acknowledgements, detector callbacks, coverage/localization state, and console status endpoints. The detailed per-PR inventory and caller/callee evidence are in `search-coverage.json`.

## Fixed findings

1. **Async search lost its coverage lifecycle after the first pending node command.** `SearchRuntime.execute` restored its navigation-completion callback when `dispatch()` returned `EXECUTING`. `AutonomySession.resume_io` then called the generic dispatcher directly. Continued completed commands could not activate coverage tasks, the search ledger was never finalized, and `SearchDetectionFactory.finish_mission()` stopped detectors immediately after the initial pending result. The fix retains the callback and worker until the terminal acknowledgement, finalizes the ledger at that point, and closes workers on a preempted search.

2. **Search reads and acknowledgement crossed session boundaries.** A console token could request a known `intent_id` under another session path and receive or acknowledge that mission. Search preview also accepted an intent whose payload session differed from the endpoint session. The fix binds preview, status, and acknowledgement to the stored mission session.

## Validation

`uv run pytest perception/test_search_events.py perception/test_search_localization.py planner/test_search.py relay/tests/test_intent_v1.py relay/tests/test_search_runtime.py relay/tests/test_search_deployment.py relay/tests/test_search_detection.py adapters/sim/test_search_demo.py relay/tests/test_search_demo_roundtrip.py -q` passed: 157 tests.

The end-to-end fake-node test now delays each `GOTO` acknowledgement and proves that the configured detector remains `running` while the search is pending. It then completes the route, records coverage and localization, and acknowledges a finding without issuing another aircraft command.

`ruff check`, `ruff format --check`, and `git diff --check` passed for the modified files.

## Per-PR outcome

| PR | Pinned delta | Production surface inspected | Result |
| --- | --- | --- | --- |
| #183 | `ea34800..096dda9` | `CoverageLedger`, evidence identity, freshness, receipts, candidates | Clean after caller review. |
| #184 | `096dda9..c43a220` | area/request validation, cell partitioning, transit and lane planning | Clean. |
| #195 | `da91dec..c47cd74` | mission preparation and frozen navigation plan | Clean. |
| #196 | `c47cd74..05ac855` | route dispatch, arrival activation, coverage lifecycle | Async continuation defect found in later composed path and fixed. |
| #198 | `05ac855..8181d77` | calibrated projection and five-frame median localization | Clean. |
| #199 | `8181d77..5b6f983` | status payload, finding storage and acknowledgement | Session ownership gap in later endpoint caller fixed. |
| #200 | `5b6f983..102ff30` | enabled search capability and strict intent arguments | Clean. |
| #202 | `102ff30..5667a8c` | search navigation admission, command arrival coverage | Clean synchronously; async caller defect fixed. |
| #203 | `5667a8c..0ba2645` | accepted-frame localization wiring | Clean. |
| #205 | `0ba2645..f43b595` | strict file-backed deployment loader | Clean. |
| #208 | `f43b595..4c430f0` | preview lease, execution binding, search HTTP endpoints | Session ownership gap fixed. |
| #209 | `4c430f0..39c1f9f` | detection factory, configured detector deployment, lifecycle ownership | Async worker termination defect fixed. |
| #210 | `39c1f9f..db24faa` | synthetic camera, pose and calibration providers | Clean. |
| #211 | `db24faa..1665b83` | fake-node round trip and reusable synthetic stream | Strengthened by delayed-ack regression test. |
| #212 | `1665b83..6acf979` | locked mission status and bounded retention | Clean. |
| #220 | `6acf979..2116190` | capability-profile sharing at relay composition and search demo assertion | Search-related wiring clean. The large language and console additions require their own slice review. |

## Slop and standards

The core search modules have no ticket archaeology, copied helper annotations, dead branches, or source-to-test cross references. The #220 console and language changes add many long explanatory docblocks. That is a style concern in the separate console/language slice, not a search runtime defect. No search behavior was changed for that concern.
