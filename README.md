# Sweep

One person commands a small drone swarm with their hands, their head, or a sentence, and sees what the swarm sees.

The first user is a responder who needs eyes inside a building before entry and whose hands are already full. The first hardware is a laptop webcam and six indoor drones; glasses and a Neural Band replace the webcam later, and natural language is the third input into the same intent bus. Everything is open source.

Status: Phase 0 (webcam gesture console plus simulator) done; the Phase 0 page itself drops into `console/public/phase0/` (see [console/README.md](console/README.md)). Phase 1 (intent bus, planner, arbiter, sim adapter, CI) starts Sept 2, 2026.

## Read first

- [PRD](docs/prd.md): problem, architecture, contracts, phases, division of labor. The four frozen contracts are [Appendix A (intent)](docs/prd.md#appendix-a-intent-contract-v1), [Appendix B (telemetry)](docs/prd.md#appendix-b-telemetry-v1), [Appendix C (adapter interface)](docs/prd.md#appendix-c-adapter-interface), and [Appendix D (repository layout)](docs/prd.md#appendix-d-repository-layout).
- [Scaffold design](docs/superpowers/specs/2026-09-01-sweep-scaffold-design.md), its [plan](docs/superpowers/plans/2026-09-01-sweep-scaffold.md), and the [docs index](docs/README.md).
- The [pull request template](.github/pull_request_template.md) is the working agreement as a checklist.

## Layout

| Path | Owner | Phase | What lives here |
|---|---|---|---|
| [`console/`](console/) | A | 0+ | Operator console: Vite + React + TypeScript |
| [`glasses/`](glasses/) | A, C | 4 | Meta Ray-Ban Display web app |
| [`relay/`](relay/) | C | 1 | FastAPI WebSocket intent bus, state, JSONL logging, replay |
| [`planner/`](planner/) | B | 1 | Deterministic formations, sweep lanes, allocation, clamping |
| [`arbiter/`](arbiter/) | B | 1 | Safety rules, e-stop, battery return |
| [`adapters/`](adapters/) | B | 1, 2 | `sim`, `crazyswarm2`, `mavlink` behind one interface |
| [`media/`](media/) | C | 3 | MediaMTX config and stream naming |
| [`perception/`](perception/) | A | 3 | Detector and world-position estimates |
| [`language/`](language/) | A, C | 5 | Plan compiler, resolvers, prompts, local fallback |
| [`evals/`](evals/) | C | 1+ | Gesture, language, sim scenario, and hardware acceptance evals |
| [`datasets/`](datasets/) | A, all | 1+ | Recorded gesture sessions and utterances |
| [`docs/`](docs/) | all | all | PRD, specs, plans, build guide, contract, demo script |
| [`tests/`](tests/) | C | all | Cross-cutting tests, starting with the layout contract test |

Owners: A is Interaction and perception, B is Autonomy and safety, C is Platform and data (PRD section 8.1). Each directory has a README with its owner, phase, responsibility, and PRD sections.

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

- A: drop the Phase 0 page into `console/public/phase0/`, then wire the console to the relay and strip its internal sim.
- B: planner and arbiter with tests, against the `sim` adapter.
- C: relay with WebSocket, token, and JSONL logging; schemas.

## Working agreement

- No merge to `main` without CI green and one review (PRD section 8.2). `main` is protected accordingly: pull request, one approval, both CI checks.
- No new intents without a contract change, a test, and all three inputs updated. No model in the safety path. Nothing off the scripted mission path until Phase 6 (PRD section 8.6).
- Stand-up 9:00, integration 16:00. Two people present for any flight, one on the e-stop keyboard; nobody flies alone. Every hardware session ends with a session report committed to the repo (PRD section 8.5).

## Remotes

- GitHub: https://github.com/worldofhacks/sweep (`origin`).
- GitLab: a public mirror on labs.gauntletai.com (`gitlab`). It is created once with `just gitlab-remote` (needs `glab` and `glab auth login --hostname labs.gauntletai.com`); on a fresh clone, add it with `git remote add gitlab <url>` instead of re-running the recipe.
