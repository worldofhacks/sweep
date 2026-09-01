# Sweep

One person commands a small drone swarm with their hands, their head, or a sentence, and sees what the swarm sees.

The first user is a responder who needs eyes inside a building before entry and whose hands are already full. The first hardware is a laptop webcam and six indoor drones; glasses and a Neural Band replace the webcam later, and natural language is the third input into the same intent bus. Everything is open source.

Status: Phase 0 (webcam gesture console plus simulator) done. Phase 1 (intent bus, planner, arbiter, sim adapter, CI) starts Sept 2, 2026.

## Read first

- [PRD](docs/prd.md): problem, architecture, contracts, phases, division of labor.
- [Scaffold design](docs/superpowers/specs/2026-09-01-sweep-scaffold-design.md) and its [plan](docs/superpowers/plans/2026-09-01-sweep-scaffold.md).

## Layout

| Path | Owner | Phase | What lives here |
|---|---|---|---|
| `console/` | A | 0+ | Operator console: Vite + React + TypeScript |
| `glasses/` | A | 4 | Meta Ray-Ban Display web app |
| `relay/` | C | 1 | FastAPI WebSocket intent bus, state, JSONL logging, replay |
| `planner/` | B | 1 | Deterministic formations, sweep lanes, allocation, clamping |
| `arbiter/` | B | 1 | Safety rules, e-stop, battery return |
| `adapters/` | B | 1, 2 | `sim`, `crazyswarm2`, `mavlink` behind one interface |
| `media/` | C | 3 | MediaMTX config and stream naming |
| `perception/` | A | 3 | Detector and world-position estimates |
| `language/` | A, C | 5 | Plan compiler, resolvers, prompts, local fallback |
| `evals/` | C | 1+ | Gesture, language, sim scenario, and hardware acceptance evals |
| `datasets/` | all | 1+ | Recorded gesture sessions and utterances |
| `docs/` | all | | PRD, specs, plans, build guide, contract, demo script |
| `tests/` | C | | Cross-cutting tests, starting with the layout contract test |

Owners: A is Interaction and perception, B is Autonomy and safety, C is Platform and data (PRD section 8.1).

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 24 with [pnpm](https://pnpm.io/), Docker, and [just](https://just.systems/).

```bash
just setup      # uv sync + pnpm install
just test       # pytest
just lint       # ruff + eslint
just console    # console dev server on http://localhost:5173
just media      # MediaMTX via docker compose
```

Python runs from the repo root through uv, and modules are invoked as packages, for example `uv run python -m relay.main` once that module exists. Keep uv's default `.venv/` at the repo root (the ignore rules assume it). `tests/test_layout.py` guards the Appendix D layout: every top-level package must resolve from this repo, and no undeclared top-level package may appear; `adapters/` subpackages are by convention.

## Working agreement

- No merge to `main` without CI green and one review (PRD section 8.2). `main` is protected accordingly.
- Contracts frozen Sept 2, 9 am: intent schema, telemetry schema, adapter interface, repo layout (PRD Appendices A to D).
- No new intents without a contract change, a test, and all three inputs updated. No model in the safety path. Nothing off the scripted mission path until Phase 6 (PRD section 8.6).
- Stand-up 9:00, integration 16:00 (PRD section 8.5).

## Remotes

- GitHub: https://github.com/worldofhacks/sweep (`origin`)
- GitLab: labs.gauntletai.com (`gitlab`), added with `just gitlab-remote`
