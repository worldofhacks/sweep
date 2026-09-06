# Navigation PR audit

Twelve pinned navigation heads were reviewed through their callers and consumers. The combined navigation suite passed 102 tests. The Python producer and the Kotlin `PythonNavigationContractTest` also passed together with a generated signed packet file. The only confirmed issue is a merge-order defect in #207: a search-enabled relay advertises a broader capability profile than the voice compiler uses, so every grounded transcript is refused as stale state. Integration #220 contains the needed shared-profile correction and its post-fix test covers both capability states.

No audit patch was created. The remaining heads have no confirmed functional defect or high-signal slop that warrants a separate cherry-pick. The producer in #217 imports private test fixtures, which is a fragile test-source dependency, but it executes from the checked-out repository in both CI definitions and does not affect relay or hardware behavior. It should be moved to a neutral fixture module if this contract producer becomes a distributed tool.

## Findings

### #207: voice compiler profile differs from the relay profile

`build_transcript_service` at `50862f4` starts from the planning profile and adds navigation. `AutonomyComposition` separately adds search when a search runtime is configured. `build_grounding_facts` correctly rejects this disagreement, but the result is that a navigation-plus-search C2 relay refuses every voice transcript before the model plan can be accepted.

The correction is already present after the reviewed head. `fa7b73e` introduces one `AutonomyConfig.effective_capability_profile`, and `b15eef4` incorporates the runtime's configured capability profile. The current navigation voice wiring test is parametrized for search on and off and passed. Merge #207 only with the #220 capability-profile lineage, or cherry-pick the corrective commits first.

## Per-PR review

| PR | Reviewed chain | Result and merge considerations |
| --- | --- | --- |
| #173 `8968178` | `ControlProvenance` serialization and `AircraftState` preservation into navigation runtime snapshots | Clean. Existing snapshots retain compatibility when provenance is absent. #174 consumes the new field. |
| #174 `1a47624` | intent validation, deterministic planner, artifact pins, `NavigationRuntime.prepare`, dispatcher and relay composition | Clean for the reviewed route-preparation path. The branch carries concurrent publisher and formation work, so merge the exact head rather than selecting only its large aggregate diff. |
| #175 `27887f6` | deployment JSON loader, artifact pinning, permissions, remote evidence and execution configuration | Clean. #176 requires the added altitude-layer contract. |
| #176 `e69a546` | dispatcher pre/post segment checks, route timeout checks, hold path, safety allow-list | Clean. It depends on #175's `max_altitude_layer_offset_m` configuration. |
| #179 `45940dd` | Kotlin `NavigationConfig` validation and arrival predicate | Clean. It is consumed by the Android node configuration, so it must precede physical-node navigation work. |
| #187 `11f8360` | relay catalog endpoint, preview storage, confirmation equality, expiry and invalidation | Clean. It requires the route runtime from #176. |
| #188 `fc87a2a` | aliases in the navigation artifact identity and deployment loader | Clean. Alias changes invalidate stale catalogs as intended. #206 consumes these aliases. |
| #206 `8d2f886` | language navigation record, destination resolver, compiler grounding and model response validation | Clean. It depends on #188 for deployed aliases and #207 for relay wiring. |
| #207 `50862f4` | transcript service factory, relay compiler, grounding facts and relay state capability profile | Confirmed merge-order defect described above. Do not merge alone into a configured C2/search relay. |
| #217 `48be395` | route authorization and pose signing, session audit/fan-out, command argument contract, Android packet producer | Clean through the Python producer and Kotlin parser. Requires the Android node consumer added elsewhere in the integration stack. |
| #218 `82bd103` | dispatcher authorization callback, remote adapter route identity, relay node delivery and node admission | Clean. Authorization and initial pose are delivered before the matching GOTO; command IDs remain scoped per dispatch. Depends on #217 and Android navigation-frame support. |
| #219 `ede28e7` | GitHub and GitLab bridge jobs, Python packet generation, JVM environment hand-off | Clean. Both CI jobs generate the packet before Gradle. The source script imports test helpers, recorded above as a maintenance risk. |

## Validation

`TMPDIR=/var/tmp/gauntlet .venv/bin/python -m pytest planner/test_navigation.py planner/test_navigation_runtime.py planner/test_navigation_deployment.py planner/test_mapped_formations.py relay/tests/test_navigation_binding.py relay/tests/test_navigation_control.py language/test_navigation_grounding.py relay/tests/test_language_navigation_wiring.py -q` passed: 102 tests.

The producer was run with `PYTHON_NAVIGATION_PACKET_PATH=/var/tmp/gauntlet/navigation-audit-packets-jvm-3.json`. It emitted a signed route, pose, and GOTO packet. With `JAVA_HOME=/var/tmp/gauntlet/jdk-21.0.12.1+1` and `JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=/var/tmp/gauntlet`, `:bridge-core:test --tests '*PythonNavigationContractTest'` reran and passed without skipping. The bridge-node test run was started but stopped after the targeted core contract result because repeated Gradle invocations were competing in the shared checkout.

All reviewed diffs passed `git show --check`. The audit did not modify either shared checkout.

## Symbol and contract addendum

`navigation-coverage.json` now records the changed public symbols and the traced callers, callees, and contracts for every pinned head. The inventory covers serialization and state provenance in #173; route preparation, artifacts, geometry, control publication, and relay configuration in #174; deployment loading in #175; segment dispatch in #176; Android arrival in #179; preview lifecycle in #187; aliases and pins in #188; language grounding in #206; voice wiring in #207; signed packet publication in #217; remote route delivery in #218; and the two JVM CI jobs in #219.

The traced boundary is continuous: approved localization and a pinned deployment produce a route preview, a confirmation consumes that exact preview, the dispatcher revalidates its segments, the relay signs the route and pose, and the remote adapter binds the route ID to its GOTO. The one break found in that chain remains #207's capability-profile mismatch. #220 supplies the shared profile used by the relay and voice compiler.

| PR | Main symbols traced | Boundary checked |
| --- | --- | --- |
| #173 | `ControlProvenance`, `AircraftState`, `Plan` | Snapshot provenance round trips to route preparation. |
| #174 | `NavigationRuntime`, artifacts, geometry, `ControlPublisher` | Pinned route preparation from approved localization. |
| #175 | `NavigationDeployment`, deployment parsers | Startup rejects invalid execution and evidence data. |
| #176 | `AdapterDispatcher`, `SafetyArbiter` | Segment revalidation, expiry, hold, and route authorization. |
| #179 | `NavigationConfig.isWithinArrival` | Node completion uses distance and uncertainty limits. |
| #187 | `AutonomySession` preview and confirmation | Exact unexpired preview reaches dispatch. |
| #188 | artifact aliases and configuration hash | Alias changes invalidate stale catalogs. |
| #206 | `NavigationGrounding`, compiler validation | Spoken names resolve to configured canonical zones. |
| #207 | `RelayTranscriptCompiler`, service factory | Relay and compiler must share one capability profile. |
| #217 | `NavigationControl`, session and packet producer | Signed route, pose, and route-bound GOTO reach Android. |
| #218 | `RemoteBridgeAdapter`, `RelayNodeLink` | Authorization precedes the matching remote GOTO. |
| #219 | GitHub and GitLab JVM jobs | CI runs the Python producer before Kotlin parses it. |
