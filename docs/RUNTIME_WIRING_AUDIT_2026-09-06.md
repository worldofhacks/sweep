# Runtime wiring audit, 2026-09-06

The integrated software rehearsal completes destination navigation and repeated
object searches through the console, an authenticated relay, and four signed
simulated nodes. Navigation reaches kitchen, lobby, both formation destinations,
and atrium. Each search covers all three configured cells, produces a location
from five camera observations, and records operator acknowledgement without an
additional flight command. The other three aircraft stay in place, and the fleet
lands at the end.

The phone path now has a relay producer for signed route authorization, navigation
pose, and route-bound GOTO packets. Android parses those packets, checks their
signatures and bindings, and applies configured navigation admission and flight
control. This is software integration evidence. Physical navigation still needs
accepted site geometry, measured localization and phone configuration, and flight
acceptance. Physical search also needs measured camera attitude and the staged
object accuracy run in issue #89.

Scope follows the GitHub issues, especially #89, #97–100, and #143–145. The MVP plan
is secondary; the historical PRD does not define current feature scope.
Capture/Worlds implementation belongs to the other team. The teammate CLI process
owns review and merges. The PR links below identify implementation and test
boundaries; an open PR does not establish deployment on main.

| Boundary | Evidence and result |
| --- | --- |
| Console buttons, keyboard and gestures | The four-aircraft fleet rehearsal exercises selection, confirmation, cancellation, reconnect and landing through signed node commands. [#164](https://github.com/worldofhacks/sweep/pull/164) supplies the isolated demo. Gesture recognizer input is explicitly synthetic. |
| Voice compilation | The relay compiler and catalog resolver bind destination aliases to configured navigation. [#206](https://github.com/worldofhacks/sweep/pull/206), [#207](https://github.com/worldofhacks/sweep/pull/207) and [#220](https://github.com/worldofhacks/sweep/pull/220) cover grounding and a shared voice/autonomy capability profile. Live provider acceptance remains separate. |
| Navigation configuration and geometry | [#169](https://github.com/worldofhacks/sweep/pull/169), [#175](https://github.com/worldofhacks/sweep/pull/175) and [#191](https://github.com/worldofhacks/sweep/pull/191) bind artifacts, validate deployment configuration, and reject blocked geometry and invalid arrival slots. |
| Frozen navigation preview | [#187](https://github.com/worldofhacks/sweep/pull/187) stores the actual server preview. Confirmation consumes that plan. Missing, expired, cancelled or mismatched previews refuse before adapter calls. |
| Navigation browser path | [#201](https://github.com/worldofhacks/sweep/pull/201) and [#213](https://github.com/worldofhacks/sweep/pull/213) exercise all five catalog destinations. The browser checks fresh arrival telemetry and stationary nonselected aircraft. |
| Search planning and execution | The stack beginning at [#195](https://github.com/worldofhacks/sweep/pull/195) freezes coverage routes and executes them through the autonomy dispatcher. Coverage comes from accepted worker frames observed during the route. Finishing a route with missing observations reports incomplete coverage. |
| Search localization | [#205](https://github.com/worldofhacks/sweep/pull/205) retains source, frame, bounding box and pose evidence. Five fresh unique projections produce a bounded median location. Findings never initiate movement. |
| Search deployment and worker lifecycle | [#208](https://github.com/worldofhacks/sweep/pull/208), [#209](https://github.com/worldofhacks/sweep/pull/209) and [#212](https://github.com/worldofhacks/sweep/pull/212) configure detection factories, reject startup failures, bound retained state and close workers at mission end. |
| Search browser path | [#216](https://github.com/worldofhacks/sweep/pull/216) and [#221](https://github.com/worldofhacks/sweep/pull/221) run two complete missions. Each requires every coverage cell and a localized finding, verifies acknowledgement adds no flight command, then checks fleet landing. Frames, camera orientation, detections and movement are synthetic. |
| Android navigation admission and control | [#180](https://github.com/worldofhacks/sweep/pull/180), [#185](https://github.com/worldofhacks/sweep/pull/185), [#186](https://github.com/worldofhacks/sweep/pull/186) and [#190](https://github.com/worldofhacks/sweep/pull/190) implement signed route/pose admission, route following, arrival, authority loss, hold/land and production setup. Navigation defaults to disabled. |
| Relay to Android publication | [#217](https://github.com/worldofhacks/sweep/pull/217) and [#218](https://github.com/worldofhacks/sweep/pull/218) publish signed evidence before mapped GOTO and invalidate terminal routes. A prior connection epoch cannot authorize a pose. [#219](https://github.com/worldofhacks/sweep/pull/219) generates Python packets in CI and runs the Kotlin consumer against them. |
| Diagnostic localization and raw sensors | [#170](https://github.com/worldofhacks/sweep/pull/170) loads measured clocks and calibration. [#178](https://github.com/worldofhacks/sweep/pull/178) repairs recording callbacks and malformed samples. Diagnostic frames retain `flight_approved=false`; the explicitly configured navigation boundary produces separate approved packets. Raw-record conversion alone does not establish live localization. |
| Audit, replay and telemetry | [#192](https://github.com/worldofhacks/sweep/pull/192), [#197](https://github.com/worldofhacks/sweep/pull/197) and [#204](https://github.com/worldofhacks/sweep/pull/204) address live replay consistency and telemetry delays caused by persistence and outbound delivery. |
| Live video | [#166](https://github.com/worldofhacks/sweep/pull/166) supplies four aircraft tiles with authenticated MediaMTX integration. Missing or changed source evidence remains unavailable until fresh proof arrives. |
| C1 and C2 releases | [#214](https://github.com/worldofhacks/sweep/pull/214), [#215](https://github.com/worldofhacks/sweep/pull/215) and [#222](https://github.com/worldofhacks/sweep/pull/222) define C2 as a strict C1 superset, add disarm authorization, and bind C2 to the simulator. [#223](https://github.com/worldofhacks/sweep/pull/223) keeps voice and console profiles consistent when navigation/search are configured. |
| C3/C4 and Capture/Worlds | These workflows remain with the other team. Destination navigation does not enable `map_area` or claim assisted-survey/capture acceptance. |

The browser proof PRs run in CI and upload screenshots, audit logs and evidence
JSON under `output/playwright/`. The final integrated console run passed all 528
tests and the production build. Navigation and search browser rehearsals also
passed with the phone and C2 changes present. The integrated Python run passed 2,201 tests in 322.81 seconds on `7d11aa4`.
Later CI repairs receive focused checks and CI on their updated PR heads.
