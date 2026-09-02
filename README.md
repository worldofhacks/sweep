# Sweep

One person commands 4 to 6 indoor drones through webcam gestures or spoken natural language, and sees what the swarm sees on a laptop console.

The first user is a responder who needs eyes inside a building before entry and whose hands are already full. The first hardware is a laptop webcam and 4 to 6 indoor drones; spoken natural language is the second control path, built right after the shared intent bus, planner, arbiter, and simulator. Glasses and an EMG band are optional Future input sources outside the core MVP. Everything is open source.

Status: the webcam gesture prototype shipped Sept 1, 2026. M0 (scope and contracts) is in progress; see [docs/mvp-plan.md](docs/mvp-plan.md) for the full M0 through M4 delivery sequence and its mapping from the earlier Phase 0 through Phase 6 labels.

## Read first

- [PRD](docs/prd.md): problem, architecture, contracts, milestones, capability areas. The four frozen contracts are [Appendix A (intent)](docs/prd.md#appendix-a-intent-contract-v1), [Appendix B (telemetry)](docs/prd.md#appendix-b-telemetry-v1), [Appendix C (adapter interface)](docs/prd.md#appendix-c-adapter-interface), and [Appendix D (repository layout)](docs/prd.md#appendix-d-repository-layout).
- [MVP delivery plan](docs/mvp-plan.md): the dependency-mapped, issue-ready work breakdown, with the legacy Phase 0 through Phase 6 mapping.
- [Scaffold design](docs/superpowers/specs/2026-09-01-sweep-scaffold-design.md), its [plan](docs/superpowers/plans/2026-09-01-sweep-scaffold.md), and the [docs index](docs/README.md).
- The [pull request template](.github/pull_request_template.md) is the working agreement as a checklist.

## Layout

| Path | Capability area | Milestone | What lives here |
|---|---|---|---|
| [`console/`](console/) | Interaction | M0+ | Operator console: Vite + React + TypeScript |
| [`glasses/`](glasses/) | Interaction, Platform | Future | Meta Ray-Ban Display web app |
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
| [`tests/`](tests/) | Platform | all | Cross-cutting tests, starting with the layout contract test |

Capability areas are module boundaries, not standing assignments: any engineer may claim a ready task (PRD section 8.1). Each directory has a README with its capability area, milestone, responsibility, and PRD sections.

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

## Sept 2, first hour

Contracts freeze at 9:00: intent schema and WebSocket topics, telemetry schema, adapter interface, repo layout (PRD section 8.2). Then, from PRD section 8.3:

- Interaction: wire the webcam console to the relay and strip its internal sim; the prototype page lives at `console/public/phase0/` (see [console/README.md](console/README.md)).
- Autonomy: planner and arbiter with tests, against the `sim` adapter.
- Platform: relay with WebSocket, token, and JSONL logging; schemas.

## Working agreement

- No merge to `main` without CI green and one review (PRD section 8.2). `main` is protected accordingly: pull request, one approval, both CI checks.
- No new intents without a contract change, a test, and every registered input updated. No model in the safety path. Nothing outside the M1 through M4 acceptance paths before M4 exits (PRD section 8.6).
- Stand-up 9:00, integration 16:00. Two people present for any flight, one on the e-stop keyboard; nobody flies alone. Every hardware session ends with a session report committed to the repo (PRD section 8.5).

## Remotes

- GitHub: https://github.com/worldofhacks/sweep (`origin`).
- GitLab: a public mirror on labs.gauntletai.com (`gitlab`). It is created once with `just gitlab-remote` (needs `glab` and `glab auth login --hostname labs.gauntletai.com`); on a fresh clone, add it with `git remote add gitlab <url>` instead of re-running the recipe.
