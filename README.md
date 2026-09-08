# Sweep

Laptop operator console: **http://127.0.0.1:5173/**. From the canonical checkout,
run `python3 tools/console.py start` (or `just console`). See
[the console operating guide](docs/laptop-console.md) for build identity,
restart, and the separate relay/video dependencies.

Sweep is a modular platform for one operator to work with a heterogeneous fleet of aerial drones and ground robots, their cameras and sensors, and optional room-world capture. Devices join through vendor adapters and declared capabilities; buttons, gestures, and language share the same intent, confirmation, telemetry, and safety path. Adding a device extends the configured fleet rather than creating a separate console.

The current integration scope includes at least five ground robots, adding at least two to the prior three. Each ground robot has two onboard cameras and one LiDAR. Each aerial drone has one camera and one owner-reported infrared depth/proximity sensor; its exact sensor model, interface, and measurements remain unverified. These are inventory requirements, not claims that every device is connected, calibrated, or ready for control. The operator console displays only real relay data and actual media; missing and expired readings remain unreported or offline. See the [modular fleet integration guide](docs/modular-fleet.md).

The first user is a responder who needs eyes inside a building before entry. Guided photo capture and private Marble room worlds remain one workflow alongside vehicle control, live video, and sensing. Existing Mini 3/RC-N1/Android bench and flight acceptance stages qualify that particular aircraft stack; their one-, two-, and four-aircraft trials do not define the platform's fleet size or qualify other devices. Known-map autonomous traversal still requires its own localization, clearance, and operator-safety evidence. The session registry preserves join, readiness, leave, loss, and rejoin history. Everything is open source.

See [docs/mvp-plan.md](docs/mvp-plan.md) for the delivery sequence and hardware qualification tracks; a milestone definition is not evidence that its hardware exit has passed.

## Read first

- [PRD](docs/prd.md): problem, architecture, contracts, milestones, capability areas. M0 freezes five contract groups: intent and WebSocket, telemetry, flight and camera adapters, repository layout, and room-world records.
- [MVP delivery plan](docs/mvp-plan.md): the dependency-mapped work breakdown.
- [Modular fleet integration](docs/modular-fleet.md): device identity, capabilities, cameras, sensors, freshness, capacity, and commissioning evidence.
- [Four-device live demo](docs/four-device-demo.md): the six-camera console workflow, confirmed gestures and language, survey evidence, parallel work and physical acceptance gates.
- [Map and navigation integration](docs/platform-integration.md): authenticated authoring, immutable approvals, active-map selection, qualified observations, and frozen destination reviews.
- [Decision records](docs/decisions/): why the scaffold and the architecture look the way they do. The [docs index](docs/README.md) lists everything else.
- The [pull request template](.github/pull_request_template.md) is the working agreement as a checklist.

## Layout

| Path | Capability area | Milestone | What lives here |
|---|---|---|---|
| [`console/`](console/) | Interaction | M0+ | Operator console: Vite + React + TypeScript |
| [`relay/`](relay/) | Platform | M1 | FastAPI WebSocket intent bus, state, JSONL logging, replay |
| [`spatial/`](spatial/) | Platform, Autonomy | M3 | Explicit frames and the shared bounded observation envelope |
| [`planner/`](planner/) | Autonomy | M1 | Deterministic formations, sweep lanes, allocation, clamping |
| [`arbiter/`](arbiter/) | Autonomy | M1 | Safety rules, e-stop, battery return |
| [`adapters/`](adapters/) | Autonomy | M1, M2 | Shared device/camera contracts, vendor bridges, and isolated simulator tests |
| [`media/`](media/) | Platform | M3 | MediaMTX config and stream naming |
| [`perception/`](perception/) | Interaction | M3 | Detector and world-position estimates |
| [`language/`](language/) | Interaction, Platform | M4 | Plan compiler, resolvers, prompts, local fallback |
| [`evals/`](evals/) | Platform | M1+ | Gesture, language, sim scenario, and hardware acceptance evals |
| [`datasets/`](datasets/) | Interaction, all | M1+ | Recorded gesture sessions and utterances |
| [`docs/`](docs/) | all | all | PRD, MVP plan, specs, plans, build guide, contract, demo script |
| [`RESEARCH/`](RESEARCH/) | all | all | Source-backed feasibility notes that constrain product claims and planning |
| [`tests/`](tests/) | Platform | all | Cross-cutting tests, starting with the layout contract test |

Capability areas define module boundaries. Any engineer may claim a ready task and own it through review (PRD section 8.1). Each runtime directory has a README with its capability area, milestone, responsibility, and PRD sections.

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 itself), Node 24 with [pnpm](https://pnpm.io/) 10 (`npm install -g pnpm@10`; the exact version is pinned in `console/package.json` and pnpm switches to it automatically), and [just](https://just.systems/). Docker only for `just media`. `glab` only if you touch the GitLab mirror.

```bash
just setup      # uv sync + pnpm install
just test       # pytest (also what bare `just` runs)
just lint       # ruff check + ruff format --check + eslint
just fmt        # auto-format and auto-fix both
just ci         # exactly what CI runs; run it before you push
just console    # single built operator console, http://127.0.0.1:5173
just media      # MediaMTX via docker compose, in the foreground
```

`just --list` shows every recipe. Python runs from the repo root through uv; `just relay` starts `relay.main` after the measured deployment settings are configured. Keep uv's default `.venv/` at the repo root (the ignore rules assume it). Copy `.env.example` to the git-ignored `.env` for explicit runtime configuration. Provider and adapter credentials stay server-side; the operator console receives its relay token and media-reader credentials through private runtime configuration, never bundled assets or URLs. `tests/test_layout.py` guards the Appendix D layout: every declared package, including the three `adapters/` subpackages, must resolve from this repo, and no undeclared top-level package may appear.

## Start here

Start with the shared intent, telemetry, adapter, camera, and membership contracts, then commission one real device and each attached feed or sensor before adding the next. New vehicle families implement declared capabilities through the same checked path; unsupported operations stay unavailable. The delivery plan retains the original Mini 3 room-capture and staged flight acceptance track, while the modular fleet track adds ground robots and their cameras and LiDAR. Simulator scenarios are isolated test evidence and never populate the operator runtime. Known-map autonomous traversal requires separate localization and collision-clearance acceptance.

## Working agreement

- No merge to `main` without CI green and one review (PRD section 8.2). `main` is protected accordingly: pull request, one approval, both CI checks.
- No new intents without a contract change, a test, and every registered input updated. No model in the safety path. Nothing outside the M1 through M4 acceptance paths before M4 exits (PRD section 8.6).
- Daily stand-up and integration. Hardware flights follow the operator and physical-RC rule in PRD section 8.5. Every hardware session ends with a session report committed to the repo.

## Remotes

- GitHub: https://github.com/worldofhacks/sweep (`origin`).
- GitLab: a public mirror on labs.gauntletai.com (`gitlab`). It is created once with `just gitlab-remote` (needs `glab` and `glab auth login --hostname labs.gauntletai.com`); on a fresh clone, add it with `git remote add gitlab <url>` instead of re-running the recipe.
