"""Run the laptop's composed physical-fleet relay on its one backend port.

The configuration is a private JSON object of environment key/string values.
No implicit dotenv, simulated backend, shared node token, or alternate port is used.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
from pathlib import Path

import uvicorn

from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.control_localization_contracts import session_identifier
from relay.main import transcript_service_factory
from relay.settings import AdapterBackend, RelaySettings, SettingsError

ROOT = Path(__file__).resolve().parents[1]
HOST, PORT = "127.0.0.1", 8010


def load_configuration(path: Path) -> tuple[dict[str, str], RelaySettings, AutonomyConfig]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise SettingsError("fleet runtime configuration must be a private regular file (0600)")
        values = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise SettingsError("cannot read the private fleet runtime JSON") from error
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()
    ):
        raise SettingsError("fleet runtime JSON must contain string keys and values")
    if values.get("SWEEP_ADAPTER_BACKEND") != "remote":
        raise SettingsError("the operator fleet requires an explicit remote backend")
    if any(key.startswith("SWEEP_SIM_") and value for key, value in values.items()):
        raise SettingsError("simulator configuration is not an operator runtime input")
    if values.get("SWEEP_RELAY_ORIGIN") != f"ws://{HOST}:{PORT}":
        raise SettingsError("the laptop relay origin must use its fixed loopback port 8010")
    try:
        session_identifier(values.get("SWEEP_SESSION_ID", ""))
    except ValueError as error:
        raise SettingsError("a new explicit fleet session ID is required") from error
    settings = RelaySettings.from_env(values)
    if (
        settings.adapter_backend is not AdapterBackend.REMOTE
        or settings.allow_shared_adapter_token
        or not settings.adapter_keys
    ):
        raise SettingsError("physical devices require distinct configured adapter credentials")
    config = AutonomyConfig.from_env(values)
    return values, settings, config


def install_environment(values: dict[str, str]) -> None:
    """Replace this process's Sweep/provider settings, retaining unrelated shell tools."""
    for key in tuple(os.environ):
        if key.startswith(("SWEEP_", "LANGFUSE_")) or key in {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        }:
            del os.environ[key]
    os.environ.update(values)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "serve"))
    parser.add_argument("--config", type=Path, default=ROOT / ".sweep/fleet/runtime.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        values, settings, config = load_configuration(args.config)
        if args.command == "check":
            print(
                json.dumps(
                    {
                        "configuration_valid": True,
                        "backend": "remote",
                        "configured_devices": len(settings.adapter_keys),
                        "configured_camera_paths": sum(
                            len(cameras) for cameras in settings.configured_cameras().values()
                        ),
                        "session": values["SWEEP_SESSION_ID"],
                        "relay": f"http://{HOST}:{PORT}",
                        "console": "http://127.0.0.1:5173/",
                        "policy": "supervised_vertical" if config.supervised_vertical else "world",
                        "hardware_qualification": "not established by configuration validation",
                    },
                    indent=2,
                )
            )
            return 0
        # Provider transports read process environment. Policy/settings above come
        # exclusively from the reviewed file, never the shell's previous session.
        install_environment(values)
        app, composition = create_autonomy_app(
            settings,
            config,
            transcript_service_factory=transcript_service_factory(config, environ=values),
        )
        try:
            uvicorn.run(app, host=HOST, port=PORT, log_level="info")
        finally:
            composition.close()
    except (SettingsError, ValueError) as error:
        # Settings errors describe fields, not secret configuration values.
        logging.getLogger(__name__).error("Fleet runtime refused: %s", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
