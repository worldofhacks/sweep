"""Composed relay process: relay, planner, arbiter, and the configured adapter backend.

Run from the repo root with the ``.env`` values in the environment (``just relay``
reads the file for you):

    uv run python -m relay.main --host 127.0.0.1 --port 8000

``relay.app:app`` stays the standalone relay that refuses every intent with
``downstream_unavailable``; this entry point is the one that dispatches.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

import uvicorn

from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.settings import RelaySettings, SettingsError

_LOGGER = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m relay.main",
        description="Run the relay with the planner, arbiter, and the adapter backend "
        "SWEEP_ADAPTER_BACKEND selects.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; keep loopback unless the LAN boundary is intentional",
    )
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    try:
        settings = RelaySettings.from_env()
        config = AutonomyConfig.from_env()
        app, composition = create_autonomy_app(settings, config)
    except SettingsError as error:
        _LOGGER.error("%s", error)
        return 2
    _LOGGER.info(
        "relay listening on %s:%s with the %s adapter backend",
        args.host,
        args.port,
        settings.adapter_backend.value,
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        composition.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
