# Sweep

One person creates AI-generated room worlds from guided photos, commands three indoor DJI Mini 3 drones through button controls on a laptop console, and sees what the swarm sees. Spoken language and gesture inputs join the same intent engine after the button-driven slice. The simulator retains the 4-to-6-drone expansion target.

The first user is a responder who needs eyes inside a building before entry. The three-guided-phone-photo Marble flow is completed feasibility evidence and remains a fallback. The first pending user-visible slice is one end-to-end drone capture: the operator clicks Capture room, reviews the Intent v1 preview, confirms it, and one DJI Mini 3 holds an approved pose while its files create a private Marble room world. The north-star command is “Map this floor.” During the MVP, it sends an operator-present three-drone swarm through approved room poses on a supplied occupancy map, then generates a room-by-room visual walkthrough. Physical bring-up uses three Mini 3 aircraft, three RC-N1 controllers, and three benchmarked Android bridge nodes, one node before three. Four to six drones remain a simulator and Future hardware target. Spoken language, gestures, and an EMG band are later input sources outside the first slice. Everything is open source.

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
| [`adapters/`](adapters/) | Autonomy | M1, M2 | deterministic simulator and DJI Mini 3 bridge contract |
| [`media/`](media/) | Platform | M3 | MediaMTX config and stream naming |
| [`perception/`](perception/) | Interaction | M3 | Detector and world-position estimates |
| [`language/`](language/) | Interaction, Platform | M4 | Plan compiler, resolvers, prompts, local fallback |
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

Contracts are frozen in M0: intent schema and WebSocket topics, telemetry schema, adapter and camera-capability interfaces, repo layout, and the room-world records (PRD section 8.2). M1 then proves one complete Mini 3 room capture and private Marble result through button-generated Intent v1. M2 adds the second and third matching bridge nodes; 4 to 6 remain in simulation. Known-map autonomous multi-room traversal and capture proves one drone before two only after indoor localization and collision-clearance sensing pass their gates. The complete dependency map is in [docs/mvp-plan.md](docs/mvp-plan.md), and any engineer may claim a ready item.

## Working agreement

- No merge to `main` without CI green and one review (PRD section 8.2). `main` is protected accordingly: pull request, one approval, both CI checks.
- No new intents without a contract change, a test, and every registered input updated. No model in the safety path. Nothing outside the M1 through M4 acceptance paths before M4 exits (PRD section 8.6).
- Daily stand-up and integration. Two people are present for any flight: one operates Sweep and one holds the physical RC safety path. The network e-stop does not replace RC takeover. Every hardware session ends with a session report committed to the repo (PRD section 8.5).

## Remotes

- GitHub: https://github.com/worldofhacks/sweep (`origin`).
- GitLab: a public mirror on labs.gauntletai.com (`gitlab`). It is created once with `just gitlab-remote` (needs `glab` and `glab auth login --hostname labs.gauntletai.com`); on a fresh clone, add it with `git remote add gitlab <url>` instead of re-running the recipe.
