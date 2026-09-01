# Sweep Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the public `sweep` repository as a bare skeleton (Appendix D layout, toolchains, one layout test, CI, compose, planning structure) on GitHub and on labs.gauntletai.com, with no application code.

**Architecture:** One git repo rooted at `/Users/quietguy/capystone`. Python side is a single uv project with flat top-level packages that are never installed (imports resolve from the repo root). Console is a Vite + React + TypeScript app in `console/`. CI runs two jobs, `python` and `console`, on GitHub Actions and mirrored on GitLab CI. Repo settings (branch protection, milestones) are applied through the GitHub API after the first push.

**Tech Stack:** uv, Python 3.12, pytest, ruff; pnpm 10.2.1, Node 24, Vite 8, React 19, TypeScript; MediaMTX 1.20.1 via docker compose; just; gh 2.65; glab 1.109.

Spec: `docs/superpowers/specs/2026-09-01-sweep-scaffold-design.md`. PRD: `docs/prd.md`.

Conventions for every commit in this plan: run `git -c commit.gpgsign=false commit`, and end the message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

## File structure

| Path | Responsibility |
|---|---|
| `pyproject.toml`, `.python-version`, `uv.lock`, `.gitignore` | Python project, pytest and ruff config, ignore rules (needed before any task runs pytest) |
| `tests/test_layout.py` | Contract test: every Appendix D Python package imports |
| `relay/`, `planner/`, `arbiter/`, `adapters/{,sim,crazyswarm2,mavlink}/`, `perception/`, `language/`, `evals/` | Empty packages, each with `__init__.py` and a README |
| `media/mediamtx.yml`, `media/README.md` | MediaMTX config and stream naming |
| `glasses/README.md`, `datasets/README.md`, `docs/README.md` | Placeholders with owner, phase, guidance |
| `console/` | Vite + React + TS app, trimmed to a placeholder page |
| `.node-version` | Node 24 for local tooling and CI |
| `docker-compose.yml` | MediaMTX service; relay and perception documented as later additions |
| `justfile`, `.env.example`, `.editorconfig` | Task runner, env template, hygiene |
| `.github/workflows/ci.yml`, `.gitlab-ci.yml`, `.github/pull_request_template.md` | CI and review rule |
| `README.md` | Entry point: layout table, quickstart, working agreement |

---

### Task 1: Python project and layout contract test

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `tests/test_layout.py`
- Create: `relay/__init__.py`, `planner/__init__.py`, `arbiter/__init__.py`, `adapters/__init__.py`, `adapters/sim/__init__.py`, `adapters/crazyswarm2/__init__.py`, `adapters/mavlink/__init__.py`, `perception/__init__.py`, `language/__init__.py`, `evals/__init__.py`

- [ ] **Step 1: Write the project file, pin Python, and add .gitignore**

`pyproject.toml`:

```toml
[project]
name = "sweep"
version = "0.0.1"
description = "One person commands a small drone swarm with their hands, their head, or a sentence, and sees what the swarm sees."
requires-python = ">=3.12,<3.13"
dependencies = []

[dependency-groups]
dev = [
  "pytest>=9,<10",
  "ruff>=0.16,<0.17",
]

[tool.uv]
package = false

[tool.pytest.ini_options]
addopts = ["--import-mode=importlib"]
pythonpath = ["."]
testpaths = ["tests", "relay", "planner", "arbiter", "adapters", "perception", "language", "evals"]

[tool.ruff]
line-length = 100
target-version = "py312"
extend-exclude = ["console", "datasets", "glasses", "*.md"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
```

`.python-version`:

```
3.12
```

`.gitignore` (ruff 0.16 would otherwise format Python fences inside Markdown, and pytest writes `__pycache__` that later `git add <dir>` steps would sweep up):

```
# Python
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
*.egg-info/
build/
dist/

# Node (console, glasses)
node_modules/

# Secrets and local config
.env
.env.*
!.env.example

# OS and editors
.DS_Store
.idea/
*.swp
```

- [ ] **Step 2: Install the dev toolchain and create the lock file**

Run: `uv sync`
Expected: creates `.venv/` and `uv.lock`; output ends with `Installed N packages` listing pytest and ruff.

- [ ] **Step 3: Write the failing layout test**

`tests/test_layout.py`:

```python
"""Contract test for the frozen repository layout (PRD Appendix D, section 8.2)."""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

PACKAGES = [
    "relay",
    "planner",
    "arbiter",
    "adapters",
    "adapters.sim",
    "adapters.crazyswarm2",
    "adapters.mavlink",
    "perception",
    "language",
    "evals",
]

TOP_LEVEL = {name for name in PACKAGES if "." not in name}

# tests/ is not part of the layout; tolerate an __init__.py there.
NOT_PACKAGES = {"tests"}


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports_from_this_repo(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__file__ is not None
    expected = REPO_ROOT.joinpath(*name.split(".")) / "__init__.py"
    assert Path(module.__file__).resolve() == expected


def test_no_undeclared_top_level_packages() -> None:
    found = {p.parent.name for p in REPO_ROOT.glob("*/__init__.py")} - NOT_PACKAGES
    assert found == TOP_LEVEL
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest tests/test_layout.py -q`
Expected: 11 failed: ten with `ModuleNotFoundError` (one per package) and `test_no_undeclared_top_level_packages`, because no package directory exists yet.

- [ ] **Step 5: Create the packages**

Each `__init__.py` holds one docstring line. Contents:

`relay/__init__.py`: `"""Intent relay: WebSocket bus, authoritative swarm state, JSONL logging, replay (owner C)."""`
`planner/__init__.py`: `"""Deterministic planner: formations, sweep lanes, allocation, clamping (owner B)."""`
`arbiter/__init__.py`: `"""Safety arbiter: validates every intent and command, owns e-stop and battery return (owner B)."""`
`adapters/__init__.py`: `"""Swarm adapters behind one interface (PRD Appendix C), owner B."""`
`adapters/sim/__init__.py`: `"""Kinematic six-drone simulator adapter, the first-class mock (Phase 1)."""`
`adapters/crazyswarm2/__init__.py`: `"""crazyswarm2 (ROS 2 Crazyflie server) adapter (Phase 2)."""`
`adapters/mavlink/__init__.py`: `"""MAVLink adapter for PX4 or ArduPilot quads via pymavlink or MAVSDK (optional)."""`
`perception/__init__.py`: `"""Perception: detector on sampled frames, detection events with world positions (owner A)."""`
`language/__init__.py`: `"""Language module: plan compiler, resolvers, prompts, local fallback (owners A and C)."""`
`evals/__init__.py`: `"""Eval harness: gesture, language, sim scenario, and hardware acceptance suites (owner C)."""`

Run to create them in one go:

```bash
mkdir -p relay planner arbiter adapters/sim adapters/crazyswarm2 adapters/mavlink perception language evals tests
printf '%s\n' '"""Intent relay: WebSocket bus, authoritative swarm state, JSONL logging, replay (owner C)."""' > relay/__init__.py
printf '%s\n' '"""Deterministic planner: formations, sweep lanes, allocation, clamping (owner B)."""' > planner/__init__.py
printf '%s\n' '"""Safety arbiter: validates every intent and command, owns e-stop and battery return (owner B)."""' > arbiter/__init__.py
printf '%s\n' '"""Swarm adapters behind one interface (PRD Appendix C), owner B."""' > adapters/__init__.py
printf '%s\n' '"""Kinematic six-drone simulator adapter, the first-class mock (Phase 1)."""' > adapters/sim/__init__.py
printf '%s\n' '"""crazyswarm2 (ROS 2 Crazyflie server) adapter (Phase 2)."""' > adapters/crazyswarm2/__init__.py
printf '%s\n' '"""MAVLink adapter for PX4 or ArduPilot quads via pymavlink or MAVSDK (optional)."""' > adapters/mavlink/__init__.py
printf '%s\n' '"""Perception: detector on sampled frames, detection events with world positions (owner A)."""' > perception/__init__.py
printf '%s\n' '"""Language module: plan compiler, resolvers, prompts, local fallback (owners A and C)."""' > language/__init__.py
printf '%s\n' '"""Eval harness: gesture, language, sim scenario, and hardware acceptance suites (owner C)."""' > evals/__init__.py
```

- [ ] **Step 6: Run the test to verify it passes, then lint**

Run: `uv run pytest -q`
Expected: `11 passed`.

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: `All checks passed!` and `11 files already formatted` (Markdown is excluded).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .python-version .gitignore uv.lock tests relay planner arbiter adapters perception language evals
git -c commit.gpgsign=false commit -m "build: uv project, ruff, and layout contract test

Flat top-level packages per PRD Appendix D; the project is not installed,
imports resolve from the repo root.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Directory READMEs and MediaMTX config

**Files:**
- Create: `relay/README.md`, `planner/README.md`, `arbiter/README.md`, `adapters/README.md`, `perception/README.md`, `language/README.md`, `evals/README.md`
- Create: `media/README.md`, `media/mediamtx.yml`
- Create: `glasses/README.md`, `datasets/README.md`, `docs/README.md`

- [ ] **Step 1: Write the Python package READMEs**

`relay/README.md`:

```markdown
# relay

Owner: C (Platform). Phase 1.

The intent bus. FastAPI with a WebSocket endpoint that accepts intents from any source, authenticates them with the shared token, stamps and logs them to append-only JSONL, forwards them to the planner, holds the authoritative swarm state, and fans state and telemetry out to consoles at 10 Hz. Exposes `/metrics` and `/session/<id>` for replay. Single process; restart-safe because state is rebuilt from adapter telemetry.

PRD: sections 4.2, 5.2, Appendix A (intent contract), Appendix B (telemetry and state fan-out).

Run from the repo root: `uv run python -m relay.<module>`.
```

`planner/README.md`:

```markdown
# planner

Owner: B (Autonomy). Phase 1.

Deterministic and unit-tested. Formations (line, column, circle, grid, V) around a center with spacing; translate; altitude; sweep lanes (lawnmower per drone, lanes assigned by current position); come home with staggered pads; hold; select. Allocation is nearest-drone-to-target. Everything is clamped to the mode's box before it becomes a command.

PRD: sections 5.3 and 5.4 (modes: indoor constrained is the capstone mode).
```

`arbiter/README.md`:

```markdown
# arbiter

Owner: B (Autonomy). Phase 1.

Pure Python, no I/O, so every rule is trivially testable. Runs on every intent and every planned command: armed state, e-stop state, geofence and ceiling, spacing minimum after the move, battery reserve for return, drone state validity, confirmation state for risky intents, operator presence. Owns two behaviors that ignore all inputs: e-stop (hover, then land if held) and battery return (return to home at reserve, land at critical).

Rule: no model in the safety path. Target: every safety rule has a test that tries to break it.

PRD: sections 4.8, 5.5, 7.3.
```

`adapters/README.md`:

```markdown
# adapters

Owner: B (Autonomy).

One interface, three implementations (PRD Appendix C):

| Package | Target | Phase |
|---|---|---|
| `sim/` | kinematic, deterministic six-drone simulator; the first-class mock used by CI and the console before hardware | 1 |
| `crazyswarm2/` | ROS 2 Crazyflie server (takeoff, go_to, land, notify_setpoints_stop, emergency) with Lighthouse or Loco positioning | 2 |
| `mavlink/` | pymavlink or MAVSDK for PX4 or ArduPilot quads | optional |

Interface: `takeoff, goto, hover, land, estop, telemetry`. Hardware is a configuration flag; every feature is built and tested against `sim` first.

PRD: sections 4.5, 5.6.
```

`perception/README.md`:

```markdown
# perception

Owner: A (Interaction and perception). Phase 3.

Samples frames at 5 to 10 fps per stream from MediaMTX, runs a small detector (YOLO-class, people and common objects; thermal if mounted), and emits detection events with a world-position estimate from drone pose and camera geometry. Detections go to the relay as events, never as commands. Confidence >= 0.6 is shown, >= 0.8 is auto-promoted to focus, nothing is auto-acted on.

PRD: sections 4.8, 5.7.
```

`language/README.md`:

```markdown
# language

Owners: A (front end) and C (LLM plumbing); B writes the resolvers. Phase 5.

Plan compiler: swarm state plus intent schema plus utterance in, an ordered plan of intents out through schema-constrained output. `validate_plan` runs, the console previews, the operator confirms, and intents are emitted one at a time through the relay. Selection and location resolvers, prompts, and a local-model fallback live here. Safety rules live in the arbiter, not in the prompt.

PRD: sections 4.3, 4.4, 4.5, 5.10.
```

`evals/README.md`:

```markdown
# evals

Owner: C (Platform). Phase 1 onward.

Four gold sets (PRD section 4.7):

1. Gesture: recorded webcam sessions with hand-labeled intent timestamps.
2. Language: 200 utterances with gold intent sequences.
3. Simulator scenarios: ten scripted missions with pass/fail assertions on final state and safety log.
4. Hardware acceptance: the scripted mission on real drones, five consecutive passes before any demo.

Sets 1 to 3 run in CI on every merge. Every bug becomes a scenario or a gold-set item before it is fixed.
```

- [ ] **Step 2: Write the media config and README**

`media/mediamtx.yml`:

```yaml
# MediaMTX configuration for Sweep (PRD section 5.7).
# Streams are named by drone id: publish to rtsp://<ground-station>:8554/drone1 ... drone6.
# The console plays them over WebRTC (WHEP) at http://<ground-station>:8889/<name>/whep
# and over HLS at http://<ground-station>:8888/<name>/.
# Every parameter not listed here keeps the MediaMTX default.

logLevel: info

rtsp: yes
webrtc: yes
hls: yes

paths:
  all_others:
```

`media/README.md`:

```markdown
# media

Owner: C (Platform). Phase 3.

MediaMTX ingests each drone's stream (RTSP, UDP, or MJPEG) and serves WebRTC and MJPEG/HLS to the console, with recording. Streams are named by drone id (`drone1` to `drone6`). Video runs on the 5 GHz band and control on 2.4 GHz.

Start it with `just media` (or `docker compose up mediamtx`). Config: `mediamtx.yml`.

PRD: sections 5.7, 7.5.
```

- [ ] **Step 3: Write the glasses, datasets, and docs READMEs**

`glasses/README.md`:

```markdown
# glasses

Owner: A (Interaction). Phase 4, starts when the glasses arrive.

Meta Ray-Ban Display web app (Meta Web Apps SDK). Renders one video feed, a minimap, and the alert line. Emits the same intents as the webcam console from pinch (select, confirm), D-pad (cycle drones, step formation), drag (altitude), head direction (translate direction, sweep box), middle pinch (cancel; held, e-stop), and Neural Handwriting (language). Needs HTTPS for the app and a WebSocket path to the relay on the same network; the shared token lives in the config page, not the URL.

Contract tests: the glasses pass the same intent tests as the webcam console (PRD section 5.1).

PRD: sections 5.9, 7.2.
```

`datasets/README.md`:

```markdown
# datasets

Owners: A records gesture sessions; all three write utterances. Phase 1 onward.

- `gesture/`: recorded webcam sessions from the console recorder with hand-labeled intent timestamps (gesture gold set).
- `utterances/`: the 200-utterance language gold set with gold intent sequences.

Recordings are large. Before committing video, track it with Git LFS:

    git lfs install
    git lfs track "datasets/**/*.mp4" "datasets/**/*.webm"
    git add .gitattributes

LFS is not configured yet; the first person to add a recording sets it up.
```

`docs/README.md`:

```markdown
# docs

- `prd.md`: the PRD, architecture, and division of labor (source of truth).
- `superpowers/specs/`: design specs, starting with the scaffold.
- `superpowers/plans/`: implementation plans.

Arriving in later phases: build guide (hardware bring-up, positioning calibration), the intent contract as generated schema docs, the demo script, and session reports from hardware runs.
```

- [ ] **Step 4: Verify ruff still passes and the YAML parses**

Run: `uv run ruff check . && uv run --with pyyaml python -c "import yaml; yaml.safe_load(open('media/mediamtx.yml')); print('mediamtx.yml ok')"`
Expected: `All checks passed!` then `mediamtx.yml ok`.

- [ ] **Step 5: Commit**

```bash
git add relay/README.md planner/README.md arbiter/README.md adapters/README.md perception/README.md language/README.md evals/README.md media glasses datasets docs/README.md
git -c commit.gpgsign=false commit -m "docs: per-directory READMEs and MediaMTX config

Owner, phase, and responsibility from PRD section 4.2 for every Appendix D
directory.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Console (Vite + React + TypeScript)

**Files:**
- Create: `console/` via create-vite, then modify `console/index.html`, `console/src/App.tsx`, `console/src/App.css`, `console/src/index.css`, `console/package.json`, `console/eslint.config.js`, `console/README.md`
- Delete: `console/src/assets/` (template demo images) and `console/public/favicon.svg`, `console/public/icons.svg` (template favicon and icon sprite)
- Create: `console/public/phase0/.gitkeep`
- Create: `.node-version`

- [ ] **Step 1: Generate the app**

Run: `pnpm dlx create-vite@9.2.0 console --template react-ts --eslint --no-immediate --no-interactive`
Expected: `Scaffolding project in /Users/quietguy/capystone/console...` then `Done.` The `--eslint` flag matters: create-vite 9 defaults React templates to Oxlint, and this plan expects `eslint.config.js` and an ESLint-based `pnpm lint`.

Run: `ls console console/src`
Expected: `eslint.config.js index.html package.json public README.md src tsconfig.app.json tsconfig.json tsconfig.node.json vite.config.ts` and `App.css App.tsx assets index.css main.tsx`. This template sets `"types": ["vite/client"]` in `tsconfig.app.json` instead of shipping `vite-env.d.ts`; `src/assets/` holds `hero.png react.svg vite.svg` and `public/` holds `favicon.svg icons.svg`.

- [ ] **Step 2: Pin Node and pnpm**

`.node-version` (repo root):

```
24
```

In `console/package.json`, set `"name": "sweep-console"`, `"version": "0.0.1"`, and `"packageManager": "pnpm@10.2.1"` at the top level. Run:

```bash
cd console && pnpm pkg set name=sweep-console version=0.0.1 packageManager=pnpm@10.2.1 && cd ..
```

- [ ] **Step 3: Replace the template page**

`console/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Sweep console</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`console/src/App.tsx`:

```tsx
import './App.css'

export default function App() {
  return (
    <main className="console">
      <h1>Sweep console</h1>
      <p>
        Operator console for the Sweep drone swarm. Map, gesture readout, ledger, video mosaic,
        focus, detections, and health strip arrive here from Phase 1 onward. All state comes from
        the relay.
      </p>
      <p>
        Phase 0&apos;s <code>swarm-gesture-console.html</code> is served unchanged from{' '}
        <code>public/phase0/</code> while it is ported into components.
      </p>
    </main>
  )
}
```

`console/src/App.css`:

```css
.console {
  max-width: 48rem;
  margin: 4rem auto;
  padding: 0 1.5rem;
  line-height: 1.5;
}
```

`console/src/index.css`:

```css
:root {
  color-scheme: light dark;
}

body {
  margin: 0;
  font-family: system-ui, sans-serif;
}
```

Remove the demo assets and reserve the Phase 0 folder:

```bash
rm -r console/src/assets
rm console/public/favicon.svg console/public/icons.svg
mkdir -p console/public/phase0 && touch console/public/phase0/.gitkeep
```

- [ ] **Step 4: Replace the template README**

`console/README.md`:

```markdown
# console

Owner: A (Interaction). Phase 0 onward.

The operator console: map, gesture readout, ledger, video mosaic, focus pane, attention promotion, health strip, and the language input with plan preview. A static web app; all state comes from the relay over WebSocket.

Stack: Vite, React, TypeScript, pnpm. Webcam hand landmarks come from MediaPipe Tasks.

    pnpm install
    pnpm dev        # http://localhost:5173
    pnpm lint
    pnpm build      # static files in dist/

Phase 0's `swarm-gesture-console.html` (ten intents, dwell and confirmations, six-drone map sim, session recording, WebSocket intent emission) drops into `public/phase0/`. Vite serves it unchanged at `/phase0/swarm-gesture-console.html` while it is ported into components. First Phase 1 job: point it at the relay instead of its internal sim.

PRD: sections 4.2, 5.8.
```

- [ ] **Step 4b: Make ESLint cover plain JavaScript files**

The template's only config object is scoped to `**/*.{ts,tsx}`, so `.js` files pass `pnpm lint` unlinted. In `console/eslint.config.js`, add this object as the last element of the array passed to `defineConfig([...])` (the `js` and `globals` imports already exist):

```js
  {
    files: ['**/*.{js,mjs,cjs}'],
    extends: [js.configs.recommended],
    languageOptions: {
      ecmaVersion: 'latest',
      globals: { ...globals.node, ...globals.browser },
    },
  },
```

Check the gate: a throwaway `console/src/lint-probe.js` containing `if (x = 2) { console.log(undefinedGlobalHere) }` must make `pnpm lint` fail with `no-cond-assign` and `no-undef`; delete it afterwards and confirm `pnpm lint` passes again.

- [ ] **Step 5: Install, lint, build**

Run: `cd console && pnpm install && pnpm lint && pnpm build && cd ..`
Expected: install ends with `Done in Ns`; lint prints nothing; build ends with `✓ built in Nms` and lists `dist/index.html` and `dist/assets/*.js`.

- [ ] **Step 6: Commit**

```bash
git add .node-version console
git -c commit.gpgsign=false commit -m "feat(console): Vite + React + TypeScript placeholder app

Trimmed create-vite react-ts template; public/phase0/ reserved for the
Phase 0 gesture console.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

`console/.gitignore` from the template excludes `node_modules` and `dist`, so neither is committed; `console/pnpm-lock.yaml` is committed.

---

### Task 4: Compose, justfile, env template, hygiene files

**Files:**
- Create: `docker-compose.yml`, `justfile`, `.env.example`, `.editorconfig`

- [ ] **Step 1: Write docker-compose.yml**

```yaml
# Ground-station services (PRD section 7.5). Start with: docker compose up
#
# relay      arrives in Phase 1 (owner C) once relay/ has an app and a Dockerfile.
# perception arrives in Phase 3 (owner A).
services:
  mediamtx:
    image: bluenviron/mediamtx:1.20.1
    container_name: sweep-mediamtx
    restart: unless-stopped
    ports:
      - "8554:8554"                 # RTSP ingest from drones and cameras
      - "8000-8001:8000-8001/udp"   # RTSP RTP/RTCP for publishers that negotiate UDP (ffmpeg's default)
      - "8889:8889"                 # WebRTC (WHIP publish, WHEP play) for the console
      - "8189:8189/udp"             # WebRTC ICE
      - "8888:8888"                 # HLS
    volumes:
      - ./media/mediamtx.yml:/mediamtx.yml:ro
      # Phase 3 (owner C): add ./recordings:/recordings and set recordPath in media/mediamtx.yml.
```

- [ ] **Step 2: Write the justfile**

```just
# Sweep task runner. List recipes with: just --list

set shell := ["bash", "-euo", "pipefail", "-c"]

default: test

# Install Python and console dependencies
setup:
    uv sync
    cd console && pnpm install

# Run the Python test suite
test:
    uv run pytest

# Lint and format-check Python and the console
lint:
    uv run ruff check .
    uv run ruff format --check .
    cd console && pnpm lint

# Auto-format and auto-fix Python
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Start the console dev server
console:
    cd console && pnpm dev

# Start MediaMTX
media:
    docker compose up mediamtx

# Create the GitLab project on labs.gauntletai.com, add the `gitlab` remote, push main.
# Requires a prior: glab auth login --hostname labs.gauntletai.com
gitlab-remote:
    glab auth status --hostname labs.gauntletai.com
    GITLAB_HOST=labs.gauntletai.com glab repo create sweep --public --remoteName gitlab --defaultBranch main --description "One person commands a small drone swarm with their hands, their head, or a sentence, and sees what the swarm sees."
    git push -u gitlab main
```

- [ ] **Step 3: Write .env.example and .editorconfig**

`.env.example`:

```
# Copy to .env (git-ignored). Keys never reach the console; only the relay and the
# language module read them (PRD section 7.2).
SWEEP_RELAY_TOKEN=
ANTHROPIC_API_KEY=
```

`.editorconfig`:

```
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
trim_trailing_whitespace = true
indent_style = space
indent_size = 2

[*.py]
indent_size = 4

[justfile]
indent_size = 4

[*.md]
trim_trailing_whitespace = false
```

- [ ] **Step 4: Verify compose parses and just lists recipes**

Run: `docker compose config --quiet && echo "compose ok" && just --list`
Expected: `compose ok` then a recipe list containing `console`, `default`, `fmt`, `gitlab-remote`, `lint`, `media`, `setup`, `test`.

Run: `just test`
Expected: `11 passed`.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml justfile .env.example .editorconfig
git -c commit.gpgsign=false commit -m "build: compose (MediaMTX), justfile, env template, hygiene files

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: CI on GitHub Actions and GitLab CI, PR template

**Files:**
- Create: `.github/workflows/ci.yml`, `.gitlab-ci.yml`, `.github/pull_request_template.md`

- [ ] **Step 1: Write the GitHub Actions workflow**

`.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  python:
    name: python
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@v10
        with:
          python-version: "3.12"
          enable-cache: true
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest

  console:
    name: console
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: console
    steps:
      - uses: actions/checkout@v7
      - uses: pnpm/action-setup@v6
        with:
          package_json_file: console/package.json
      - uses: actions/setup-node@v7
        with:
          node-version-file: .node-version
          cache: pnpm
          cache-dependency-path: console/pnpm-lock.yaml
      - run: pnpm install --frozen-lockfile
      - run: pnpm lint
      - run: pnpm build
```

- [ ] **Step 2: Write the GitLab CI mirror**

`.gitlab-ci.yml`:

```yaml
# Mirror of .github/workflows/ci.yml for the labs.gauntletai.com copy.
stages: [test]

python:
  stage: test
  image: ghcr.io/astral-sh/uv:python3.12-bookworm-slim
  variables:
    UV_LINK_MODE: copy
  script:
    - uv sync --locked
    - uv run ruff check .
    - uv run ruff format --check .
    - uv run pytest

console:
  stage: test
  image: node:24-bookworm-slim
  before_script:
    - npm install -g pnpm@10.2.1
  script:
    - cd console
    - pnpm install --frozen-lockfile
    - pnpm lint
    - pnpm build
```

- [ ] **Step 3: Write the PR template**

`.github/pull_request_template.md`:

```markdown
## What

<!-- One or two sentences. Link the milestone or issue. -->

## Checklist (PRD sections 8.2 and 8.6)

- [ ] CI is green (`python` and `console`)
- [ ] One review from another engineer
- [ ] No new intent without a contract change, a test, and all three inputs updated
- [ ] No model in the safety path
- [ ] On the scripted mission path, or Phase 6 hardening
```

- [ ] **Step 4: Verify both CI files parse**

Run: `uv run --with pyyaml python -c "import yaml; [yaml.safe_load(open(f)) for f in ('.github/workflows/ci.yml', '.gitlab-ci.yml')]; print('ci yaml ok')"`
Expected: `ci yaml ok`.

- [ ] **Step 5: Commit**

```bash
git add .github .gitlab-ci.yml
git -c commit.gpgsign=false commit -m "ci: python and console jobs on GitHub Actions and GitLab CI

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Root README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write the README**

````markdown
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

Python runs from the repo root through uv, and modules are invoked as packages, for example `uv run python -m relay.main` once that module exists.

## Working agreement

- No merge to `main` without CI green and one review (PRD section 8.2). `main` is protected accordingly.
- Contracts frozen Sept 2, 9 am: intent schema, telemetry schema, adapter interface, repo layout (PRD Appendices A to D).
- No new intents without a contract change, a test, and all three inputs updated. No model in the safety path. Nothing off the scripted mission path until Phase 6 (PRD section 8.6).
- Stand-up 9:00, integration 16:00 (PRD section 8.5).

## Remotes

- GitHub: https://github.com/worldofhacks/sweep (`origin`)
- GitLab: labs.gauntletai.com (`gitlab`), added with `just gitlab-remote`
````

- [ ] **Step 2: Verify the links resolve**

Run: `for f in docs/prd.md docs/superpowers/specs/2026-09-01-sweep-scaffold-design.md docs/superpowers/plans/2026-09-01-sweep-scaffold.md; do test -f "$f" && echo "ok $f"; done`
Expected: three `ok` lines.

- [ ] **Step 3: Commit**

```bash
git add README.md
git -c commit.gpgsign=false commit -m "docs: root README with layout, quickstart, working agreement

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Push to GitHub and confirm CI

**Files:** none (remote operations)

- [x] **Steps 1 and 2 done early** (2026-09-01, after Task 2, at the user's request): `gh repo create worldofhacks/sweep --public --source=. --remote=origin --push` created https://github.com/worldofhacks/sweep with `origin` tracking `main`. From here on, every task pushes with `git push origin main` right after its commit, so the remote never lags the local tree.

- [ ] **Step 3: Wait for CI and confirm both jobs are green**

Run: `sleep 20; gh run list --branch main --limit 1 --json databaseId --jq '.[0].databaseId'` then `gh run watch <id> --exit-status`
Expected: both `python` and `console` jobs end with `✓`, and the command exits 0. If a job fails, read `gh run view <id> --log-failed`, fix locally, commit, `git push`, and repeat this step.

---

### Task 8: Branch protection and phase milestones

**Files:** none (GitHub API)

- [ ] **Step 1: Protect main**

```bash
gh api -X PUT repos/worldofhacks/sweep/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["python", "console"] },
  "enforce_admins": false,
  "required_pull_request_reviews": { "required_approving_review_count": 1, "dismiss_stale_reviews": true },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

Verify: `gh api repos/worldofhacks/sweep/branches/main/protection --jq '{checks: .required_status_checks.contexts, reviews: .required_pull_request_reviews.required_approving_review_count, admins: .enforce_admins.enabled}'`
Expected: `{"checks":["python","console"],"reviews":1,"admins":false}`.

- [ ] **Step 2: Create the six milestones**

```bash
m() { gh api -X POST repos/worldofhacks/sweep/milestones -f title="$1" -f due_on="$2" -f description="$3" --jq '.number + 0 | tostring + " " + "'"$1"'"'; }
m "Phase 1: Intent bus, planner, arbiter, sim adapter, CI" "2026-09-04T12:00:00Z" "Entry: intent schema frozen (morning of Sept 2). Exit: the scripted mission runs end to end through relay, planner, arbiter, and sim, driven by webcam gestures, with zero unsafe intents in the log and the sim suite green in CI. PRD section 6."
m "Phase 2: Real drones, indoor" "2026-09-09T12:00:00Z" "Entry: drone model known, positioning chosen, flight space set up with netting or guards. Exit: the scripted mission completes hands-free on six drones five times in a row; the safety log shows correct refusals for a deliberately unsafe intent. PRD section 6."
m "Phase 3: Video and perception" "2026-09-12T12:00:00Z" "Entry: one camera source available. Exit: focus on a drone by holding up its number; a detection promotes its feed within one second; video latency within budget on all six. PRD section 6."
m "Phase 4: Glasses and Neural Band" "2026-09-17T12:00:00Z" "Entry: glasses in hand, developer mode on, relay reachable from the glasses' network. Exit: the scripted mission completes from the glasses with hands at the sides, video visible in the lens, and the safety log identical in shape to the webcam run. PRD section 6."
m "Phase 5: Natural language" "2026-09-19T12:00:00Z" "Entry: intent contract stable; relay exposes state to the language module. Exit: plan accuracy of at least 85 percent on the gold set, zero unsafe intents, three multi-step orders demonstrated on real drones. PRD section 6."
m "Phase 6: Hardening, demo, release" "2026-09-24T12:00:00Z" "Deliverables: failure-mode drills on hardware, adversarial tests, documentation and build guide, release, demo script and recorded reel. Exit: five consecutive scripted runs on hardware with no safety intervention; public repository tagged v0.1; demo reel cut. PRD section 6."
```

Verify: `gh api repos/worldofhacks/sweep/milestones --jq '.[] | "\(.number) \(.due_on[:10]) \(.title)"'`
Expected: six lines, numbers 1 to 6, due dates 09-04, 09-09, 09-12, 09-17, 09-19, 09-24.

---

### Task 9: GitLab project on labs.gauntletai.com

**Files:** none (remote operations). Blocked until the user has run `glab auth login --hostname labs.gauntletai.com`.

- [ ] **Step 1: Check authentication**

Run: `glab auth status --hostname labs.gauntletai.com 2>&1 | head -3`
Expected when ready: `✓ Logged in to labs.gauntletai.com as <user>`. If it still reports `401 Unauthorized`, stop here, report that GitLab is pending on the user's login, and leave `just gitlab-remote` as the one-command finish.

- [ ] **Step 2: Create the project, add the remote, push**

Run: `just gitlab-remote`
Expected: `✓ Created repository ... on GitLab` (or equivalent), then `branch 'main' set up to track 'gitlab/main'`.

- [ ] **Step 3: Verify both remotes**

Run: `git remote -v`
Expected: `origin https://github.com/worldofhacks/sweep.git` and `gitlab https://labs.gauntletai.com/<namespace>/sweep.git`, each for fetch and push.

- [ ] **Step 4: Record the GitLab URL in the README and push to both**

Edit the last line of `README.md` under Remotes to `- GitLab: https://labs.gauntletai.com/<namespace>/sweep (\`gitlab\`)` using the namespace from Step 3. Then:

```bash
git add README.md
git -c commit.gpgsign=false commit -m "docs: record GitLab remote

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push origin main && git push gitlab main
```

`main` is protected on GitHub with admin bypass on, so this direct push from the owner account succeeds; everyone else goes through a pull request.

---

## Self-review against the spec

- Spec section 2 decisions: repo root and `main` (Task 7), GitHub `origin` (Task 7), GitLab `gitlab` (Task 9), Vite + React + TS (Task 3), uv / Python 3.12 / pytest / ruff (Task 1), `package = false` and root imports (Task 1), bare skeleton depth (all), no license files (none created anywhere), CI on both hosts (Task 5), branch protection (Task 8), milestones (Task 8).
- Spec section 3 layout: every Appendix D directory has a README and, where Python, an `__init__.py` (Tasks 1, 2, 3); `tests/test_layout.py` (Task 1); root files `pyproject.toml`, `uv.lock`, `justfile`, `docker-compose.yml`, `.github/`, `.gitlab-ci.yml`, `.gitignore`, `.editorconfig`, `.python-version`, `.node-version`, `.env.example`, `README.md` (Tasks 1, 3, 4, 5, 6).
- Spec section 4: pytest `pythonpath` and `testpaths` (Task 1), ruff rules and excludes (Task 1), console scripts unchanged and `packageManager` pinned (Task 3), compose ports and mount (Task 4), justfile recipes `setup test lint fmt console media gitlab-remote` (Task 4), `.env.example` keys (Task 4), CI jobs and steps (Task 5), PR template (Task 5), protection settings and milestone dates (Task 8).
- Spec section 5 verification: pytest (Task 1 step 6), ruff (Task 1 step 6), pnpm lint and build (Task 3 step 5), GitHub jobs green (Task 7 step 3), `gh repo view` and `git remote -v` (Tasks 7 and 9).
- Placeholder scan: no TBD or TODO; every code step shows its content; the only conditional is Task 9, which depends on the user's login and states what to do in each case.
- Name consistency: job names `python` and `console` in `ci.yml` match the protection contexts in Task 8; recipe `gitlab-remote` in Task 4 matches Task 9 and the README; the plan filename referenced in the README matches this file.
