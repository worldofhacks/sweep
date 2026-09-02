# Sweep scaffold design

Date: 2026-09-01
Status: approved in session (bare skeleton)
Product source of truth: `docs/prd.md` (Sweep PRD v0.2, Sept 1, 2026)

## 1. Purpose

Stand up the public `sweep` repository so the three engineers (A: Interaction, B: Autonomy, C: Platform) can freeze contracts on the morning of Sept 2 and start Phase 1 in a repo that already has the agreed layout, toolchains, CI, and planning structure. The scaffold contains no application code.

## 2. Decisions made in this session

| Decision | Choice | Why |
|---|---|---|
| Repo root | this directory (`/Users/quietguy/capystone`), default branch `main` | the user's stated project directory |
| GitHub | public `worldofhacks/sweep`, remote `origin` | the authenticated `gh` account |
| GitLab | public project on `labs.gauntletai.com`, remote `gitlab` | user's choice; the course GitLab |
| Console toolchain | Vite 8 + React 19 + TypeScript, pnpm | user's choice; still emits static files |
| Python toolchain | uv, Python 3.12 (`>=3.12,<3.13`), pytest, ruff | ROS 2 Jazzy compatibility for B |
| Python packaging | flat top-level packages per Appendix D; project not installed (`tool.uv.package = false`); imports resolve from the repo root | keeps the frozen layout literal; avoids installing generic names such as `evals` into site-packages |
| Scaffold depth | bare skeleton: layout, READMEs, toolchains, one layout test, CI, compose | user's choice |
| Licensing | no license files in the scaffold; a license is chosen at the Phase 6 release | user's direction |
| CI | GitHub Actions `python` and `console` jobs; `.gitlab-ci.yml` mirrors them | PRD section 7.5; keeps the GitLab copy live |
| Branch protection | `main` requires a pull request, one approval, and the `python` and `console` checks; admins may bypass | PRD section 8.2 rule 4 |
| Milestones | six GitHub milestones, Phases 1 to 6, PRD dates | so issues can be filed on Sept 2 |

## 3. Layout

Appendix D of the PRD, verbatim, plus one root `tests/` directory.

| Path | Owner | Phase | Contents in this scaffold |
|---|---|---|---|
| `console/` | A | 0 onward | Vite + React + TS app; placeholder page naming where `swarm-gesture-console.html` drops in |
| `glasses/` | A, C | 4 | README only |
| `relay/` | C | 1 | empty Python package |
| `planner/` | B | 1 | empty Python package |
| `arbiter/` | B | 1 | empty Python package |
| `adapters/` | B | 1 and 2 | empty Python package with `sim/`, `crazyswarm2/`, `mavlink/` subpackages |
| `media/` | C | 3 | minimal `mediamtx.yml`; README on stream naming by drone id |
| `perception/` | A | 3 | empty Python package |
| `language/` | A and C | 5 | empty Python package |
| `evals/` | C | 1 onward | empty Python package; README naming the four gold sets |
| `datasets/` | A, all | 1 onward | README with Git LFS guidance |
| `docs/` | all | now | `prd.md`, this spec, the plan, index README |
| `tests/` | C | now | `test_layout.py`, which imports every Python package from this repo and rejects undeclared top-level packages |
| root | C | now | `pyproject.toml`, `uv.lock`, `justfile`, `docker-compose.yml`, `.github/`, `.gitlab-ci.yml`, `.gitignore`, `.editorconfig`, `.python-version`, `.node-version`, `.env.example`, `README.md` |

Each directory README states the owner, phase, and responsibility from PRD section 4.2 in a few lines.

## 4. Component details

### Python

- `pyproject.toml`: name `sweep`, version `0.0.1`, no runtime dependencies, dev dependency group with `pytest` and `ruff` bounded to the locked minor versions.
- `[tool.uv] package = false`. `[tool.pytest.ini_options]` sets `pythonpath = ["."]` and a `testpaths` list of `tests` plus the seven package directories, so package-local tests are collected later.
- `[tool.ruff]`: line length 100, target py312, rule sets E, F, I, B, UP; `console/`, `datasets/`, `glasses/`, and `*.md` excluded (ruff's formatter rewrites Python fences inside Markdown).
- Everything runs from the repo root through `uv run`, with modules invoked as `python -m package.module`.

### Console

- Created with `pnpm create vite console --template react-ts`; `packageManager` pinned to the locally installed pnpm.
- `App.tsx` replaced by a titled placeholder; template demo assets removed; `index.html` title set to "Sweep console".
- Scripts unchanged from the template: `dev`, `build` (`tsc -b && vite build`), `lint`, `preview`.

### Infra

- `docker-compose.yml`: project name `sweep`, no restart policy, and a `mediamtx` service pinned to `bluenviron/mediamtx:1.20.1` exposing RTSP 8554 plus RTP/RTCP 8000 to 8001/udp, WebRTC 8889 plus 8189/udp, and HLS 8888, with the config mounted from `media/mediamtx.yml`. Comments mark where `relay` (Phase 1) and `perception` (Phase 3) are added.
- `justfile` recipes: `setup`, `test`, `lint`, `ci` (exactly what CI runs), `fmt`, `console`, `media`, `gitlab-remote` (pushes without `-u`, so `main` keeps `origin` as upstream).
- `.env.example` with `SWEEP_RELAY_TOKEN` and `ANTHROPIC_API_KEY`, both empty, per PRD section 7.2.

### CI

- `.github/workflows/ci.yml` runs on pushes to `main` and on pull requests, with read-only token permissions, 10-minute job timeouts, and a concurrency group that cancels superseded runs except on `main`. Job `python`: setup-uv, `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest`. Job `console`: pnpm and Node 24, `pnpm install --frozen-lockfile`, `pnpm lint`, `pnpm build`.
- `.gitlab-ci.yml`: the same two jobs on the official `uv` and `node:24` images, limited to merge requests and the default branch, interruptible, with 10-minute timeouts and the pnpm version read from `console/package.json`.
- `.github/pull_request_template.md`: CI green, one review, the PRD section 8.6 rules, and no frozen-contract change without agreement, as a checklist.

### Repo settings

- Branch protection on `main` as in section 2, applied after the first push.
- Milestones: Phase 1 due 2026-09-04, Phase 2 due 2026-09-09, Phase 3 due 2026-09-12, Phase 4 due 2026-09-17, Phase 5 due 2026-09-19, Phase 6 due 2026-09-24, each described by its PRD entry criterion and exit test.

## 5. Verification

- `uv run pytest` passes locally (the layout test).
- `uv run ruff check .` and `uv run ruff format --check .` pass.
- `pnpm lint` and `pnpm build` pass in `console/`.
- Both GitHub Actions jobs are green on the first push to `main`.
- `gh repo view worldofhacks/sweep` reports public; `git remote -v` shows `origin` and, once authenticated, `gitlab`.

## 6. Out of scope

- Contracts as code (Pydantic models, JSON Schema, the adapter Protocol) and any relay, planner, arbiter, sim, perception, or language code. These start on Sept 2 per PRD section 8.3.
- The Phase 0 file `swarm-gesture-console.html` is not on this machine, so `console/` documents where it goes.
- CODEOWNERS: the GitHub handles of engineers A and B are unknown; add it when they are.
- Git LFS for recordings: recommended in `datasets/README.md`, not configured.

## 7. Open item

- GitLab creation and push wait on `glab auth login --hostname labs.gauntletai.com`, which only the user can run. The `just gitlab-remote` recipe performs the creation, the remote add, and the push afterwards.
