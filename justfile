# Sweep task runner. List recipes with: just --list

set shell := ["bash", "-euo", "pipefail", "-c"]

# Run the Python test suite (default)
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

# Run exactly what CI runs (locked sync, lint, format check, tests, console install/lint/build)
ci:
    uv sync --locked
    uv run ruff check .
    uv run ruff format --check .
    uv run pytest
    cd console && pnpm install --frozen-lockfile && pnpm lint && pnpm test && pnpm build

# Auto-format and auto-fix Python and the console
fmt:
    uv run ruff format .
    uv run ruff check --fix .
    cd console && pnpm lint --fix

# Start the console dev server
console:
    cd console && pnpm dev

# Start MediaMTX in the foreground (Ctrl-C stops it)
media:
    docker compose up mediamtx

# Run the relay with the planner, arbiter, and the SWEEP_ADAPTER_BACKEND adapters; reads .env
relay host="127.0.0.1" port="8000": _dotenv
    uv run --env-file .env python -m relay.main --host {{host}} --port {{port}}

# Connect a fake bridge node to a running relay (Ctrl-C stops it); reads .env credentials
fake-node drone_id="1" session="demo" relay="ws://127.0.0.1:8000": _dotenv
    uv run --env-file .env python -m adapters.dji_mini3.fake_node --drone-id {{drone_id}} --session {{session}} --relay {{relay}}

_dotenv:
    @test -f .env || { echo "copy .env.example to .env and fill in SWEEP_RELAY_TOKEN first"; exit 1; }

# Requires a prior: glab auth login --hostname labs.gauntletai.com
# Create the GitLab project on labs.gauntletai.com, add the `gitlab` remote, push main
gitlab-remote:
    glab auth status --hostname labs.gauntletai.com
    GITLAB_HOST=labs.gauntletai.com glab repo create sweep --public --remoteName gitlab --defaultBranch main --description "One person commands 4 to 6 indoor drones through webcam gestures or spoken natural language, and sees what the swarm sees on a laptop console."
    git push gitlab main
