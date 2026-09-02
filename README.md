# Sweep

One person creates AI-generated room worlds from guided photos, commands 4 to 6 indoor drones through webcam gestures or spoken natural language, and sees what the swarm sees on a laptop console.

The first user is a responder who needs eyes inside a building before entry and whose hands are already full. The first user-visible software slice lets a person take three guided, overlapping photos of one empty room and receive a private Marble room world. The north-star command is “Map this floor.” During the MVP, it sends an operator-present swarm through approved room poses on a supplied occupancy map, then generates a room-by-room visual walkthrough. The flight path starts with a laptop webcam and two indoor drones, expands to 4 to 6 drones, and adds spoken natural language after the shared intent bus, planner, arbiter, and simulator. An EMG band is an optional Future input source outside the core MVP. Everything is open source.

Status: M0 (scope and contracts) is in progress; see [docs/mvp-plan.md](docs/mvp-plan.md) for the M0 through M4 delivery sequence.

## Read first

- [PRD](docs/prd.md): problem, architecture, contracts, milestones, capability areas. M0 freezes five contract groups: intent and WebSocket, telemetry, flight and camera adapters, repository layout, and room-world records.
- [MVP delivery plan](docs/mvp-plan.md): the dependency-mapped work breakdown.
- [Decision records](docs/decisions/): why the scaffold and the architecture look the way they do. The [docs index](docs/README.md) lists everything else.
- The [pull request template](.github/pull_request_template.md) is the working agreement as a checklist.

## Layout

| Path | Capability area | Milestone | What lives here |
|---|---|---|---|
| [`console/`](console/) | Interaction | M0+ | Operator console: Vite + React + TypeScript |
| [`relay/`](relay/) | Platform | M1 | FastAPI WebSocket intent bus, state, JSONL logging, replay |
| [`planner/`](planner/) | Autonomy | M1 | Deterministic formations, sweep lanes, allocation, clamping |
| [`arbiter/`](arbiter/) | Autonomy | M1 | Safety rules, e-stop, battery return |
| [`adapters/`](adapters/) | Autonomy | M1, M2 | `sim`, `crazyswarm2`, `mavlink` behind one interface |
| [`media/`](media/) | Platform | M3 | MediaMTX config and stream naming |
| [`perception/`](perception/) | Interaction | M3 | Detector and world-position estimates |
| [`language/`](language/) | Interaction, Platform | M1, M4 | Plan compiler, resolvers, prompts, local fallback |
| [`evals/`](evals/) | Platform | M1+ | Gesture, language, sim scenario, and hardware acceptance evals |
| [`datasets/`](datasets/) | Interaction, all | M1+ | Recorded gesture sessions and utterances |
| [`docs/`](docs/) | all | all | PRD, MVP plan, specs, plans, build guide, contract, demo script |
| [`RESEARCH/`](RESEARCH/) | all | all | Source-backed feasibility notes that constrain product claims and planning |
| [`tests/`](tests/) | Platform | all | Cross-cutting tests, starting with the layout contract test |

Capability areas are module boundaries, not standing assignments: any engineer may claim a ready task (PRD section 8.1). Each runtime directory has a README with its capability area, milestone, responsibility, and PRD sections.

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12 itself), Node 24 with [pnpm](https://pnpm.io/) 10 (`npm install -g pnpm@10`; the exact version is pinned in `console/package.json` and pnpm switches to it automatically), and [just](https://just.systems/). Docker only for `just media`. `glab` only if you touch the GitLab mirror.

```bash
just setup      # uv sync + pnpm install
just test       # pytest (also what bare `just` runs)
just lint       # ruff check + ruff format --check + eslint
just fmt        # auto-format and auto-fix both
just ci         # exactly what CI runs; run it before you push
just console    # console dev server, http://localhost:5173 by default
just media      # MediaMTX via docker compose, in the foreground
```

`just --list` shows every recipe. Python runs from the repo root through uv, and modules are invoked as packages, for example `uv run python -m relay.main` once that module exists. Keep uv's default `.venv/` at the repo root (the ignore rules assume it). Copy `.env.example` to `.env` when you need the relay token or the API key; keys never reach the console. `tests/test_layout.py` guards the Appendix D layout: every declared package, including the three `adapters/` subpackages, must resolve from this repo, and no undeclared top-level package may appear.

## Start here

Contracts are frozen in M0: intent schema and WebSocket topics, telemetry schema, adapter and camera-capability interfaces, repo layout, and the room-world records (PRD section 8.2). After the World API access spike passes, the three-photo room-world slice can proceed beside the control path. The control work order remains contracts, the two-drone sim path, one real drone, two real drones, and one selected live feed. Language, the 4-to-6-drone expansion, and one-drone room capture follow the M2.0 checkpoint; known-map collection then proves one drone before two. The complete dependency map is in [docs/mvp-plan.md](docs/mvp-plan.md), and any engineer may claim a ready item.

## Working agreement

- No merge to `main` without CI green and one review (PRD section 8.2). `main` is protected accordingly: pull request, one approval, both CI checks.
- No new intents without a contract change, a test, and every registered input updated. No model in the safety path. Nothing outside the M1 through M4 acceptance paths before M4 exits (PRD section 8.6).
- Daily stand-up and integration. Two people present for any flight, one on the e-stop keyboard; nobody flies alone. Every hardware session ends with a session report committed to the repo (PRD section 8.5).

## Remotes

- GitHub: https://github.com/worldofhacks/sweep (`origin`).
- GitLab: a public mirror on labs.gauntletai.com (`gitlab`). It is created once with `just gitlab-remote` (needs `glab` and `glab auth login --hostname labs.gauntletai.com`); on a fresh clone, add it with `git remote add gitlab <url>` instead of re-running the recipe.
