# Sweep

Sweep lets an operator select indoor drones, preview a destination or search, confirm the request, and follow execution from a laptop console. Buttons, keyboard controls, gestures and voice share an Intent v1 boundary with a deterministic planner, safety arbiter and vehicle adapters. Detections report findings; they never initiate approach, following or other movement.

The physical target is four DJI Mini 3 aircraft with one Android bridge and physical RC safety operator per airborne aircraft. Four to six aircraft are exercised in simulation. A separate Capture/Worlds workflow turns accepted capture bundles into room-world presentations; generated content never supplies flight geometry.

## Current status

As of September 6, 2026, the integrated PR stack passes 2,201 Python tests, 528 console tests, the production console build, and browser rehearsals for fleet controls, five destination routes and repeated object search. The search rehearsal covers every configured cell, localizes findings from five synthetic observations and checks that acknowledgement adds no flight command. [The runtime audit](https://github.com/worldofhacks/sweep/pull/224) links each implementation and test boundary. Open PRs still require review and merge; these results describe the tested integration checkout.

Physical flight authorization remains pending. One real aircraft must supply measured camera, velocity, height, attitude/gimbal and timing evidence, including dropouts. Five mapped-route rehearsals must show p95 position error at or below 0.25 m against independent reference measurements, with no unhandled update gap over 500 ms. Map/calibration binding, stale-data refusal, hold/land behavior and physical RC takeover must pass their deployment and failure trials. Estimator confidence and synthetic tests cannot substitute for those measurements.

The remaining input integration is concrete: Android sensor callbacks expose receipt time, and the current camera path lacks verified capture-time and synchronized body/gimbal transforms. Recording and diagnostic conversion preserve that distinction. The live ingestion and estimator timing contract must be completed against measured source behavior before those inputs can earn control authority. [Acceptance evaluator #226](https://github.com/worldofhacks/sweep/pull/226) compares recorded poses with independent references; it does not certify raw sensor timing.

The current work prioritizes autonomous destination navigation and object search. Lobby and atrium-front are candidate formation volumes pending measured approval; kitchen remains a named destination/transit area and is not a formation fallback. Capture/Worlds continues in its separate lane.

## Read first

- [GitHub issues](https://github.com/worldofhacks/sweep/issues): authoritative feature scope and acceptance decisions.
- [MVP delivery plan](docs/mvp-plan.md): current priorities, issue map, dependency boundaries and remaining physical gates.
- [PRD](docs/prd.md): product behavior, architecture and contracts, synchronized with the issues.
- [Decision records](docs/decisions/) and [docs index](docs/README.md): supporting rationale and run guides.

C1 provides earned basic controls, including selected land and configured altitude. C2 adds disarm, formations, spacing and sweep for accepted deployments. Configured `navigate {zone_id}` and `search {zone_id, target_label}` remain separate from C3 assisted survey and C4 `map_area` traversal/capture. Navigation arrival permission does not authorize a formation.

## Run the software

Install [uv](https://docs.astral.sh/uv/), Node at the version in [.node-version](.node-version), [pnpm](https://pnpm.io/) at the version in [console/package.json](console/package.json), and [just](https://just.systems/). Docker is needed for MediaMTX.

```bash
just setup
just test
just lint
just ci
```

`just ci` runs the Python and console checks. GitHub CI also runs the browser and applicable JVM/Android checks. For the existing browser-to-simulator path:

```bash
pnpm --dir console exec playwright install chromium
pnpm --dir console test:m14-browser
```

For an interactive relay and console, copy `.env.example` to `.env`, configure the required credentials and backend, then run `just relay` and `just console` in separate terminals. See the [relay guide](relay/README.md) for session and adapter configuration. Keep credentials on the server and out of browser bundles and logs.

The extended fleet, navigation and search rehearsals require their integration changes while those PRs remain open. [Navigation proof #213](https://github.com/worldofhacks/sweep/pull/213) and [search proof #221](https://github.com/worldofhacks/sweep/pull/221) contain the run commands and upload screenshots, audit logs and evidence JSON in CI. Their loopback demos use generated credentials and explicitly synthetic maps, camera inputs and aircraft movement.

## Layout

| Path | Responsibility |
| --- | --- |
| [`console/`](console/) | Operator controls, previews, fleet/media state and findings |
| [`relay/`](relay/) | Authentication, sessions, capability state, signed transport, audit and replay |
| [`planner/`](planner/) | Deterministic intent planning, routes, arrival slots and fleet operations |
| [`arbiter/`](arbiter/) | State, clearance, separation, confirmation and stop checks |
| [`adapters/`](adapters/) | Simulator and DJI phone bridge implementations |
| [`perception/`](perception/) | Tag localization, fusion, detection and observation provenance |
| [`media/`](media/) | MediaMTX configuration and stream transport |
| [`language/`](language/) | Transcript compilation, authoritative grounding and producer evaluation |
| [`evals/`](evals/), [`tests/`](tests/) | Runtime, contract and acceptance checks |
| [`datasets/`](datasets/) | Recorded inputs and evaluation cases |
| [`docs/`](docs/), [`RESEARCH/`](RESEARCH/) | Product plans, protocols, run guides and supporting research |

## Working agreement

Use small PRs with current CI and independent review. Shared contracts and safety paths have one change owner and a separate reviewer. New or widened intents require contract and execution tests; no input producer calls an adapter directly, and no model decides safety authorization.

Record the exact code, map, calibration and hardware configuration for each acceptance run. Real flights retain operator presence and physical RC takeover throughout. Issue status, software implementation and physical acceptance are separate claims.

## Remotes

[GitHub](https://github.com/worldofhacks/sweep) is `origin`. The public GitLab mirror is on labs.gauntletai.com; `just gitlab-remote` creates it once, while an existing mirror should be added with `git remote add gitlab <url>`.
