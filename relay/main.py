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
import os
from collections.abc import Callable, Mapping, Sequence

import uvicorn

from language.navigation import NavigationGrounding, navigation_from_record
from language.relay_compiler import RelayTranscriptCompiler
from language.transport import AnthropicTransport, ModelTransport
from planner.models import TranslationPolicy
from relay.app import RelayRuntime
from relay.autonomy import AutonomyConfig, create_autonomy_app
from relay.capabilities import CapabilityProfile, IntentName
from relay.navigation_metadata import navigation_metadata
from relay.settings import RelaySettings, SettingsError
from relay.voice import TranscriptionTransport, TranscriptService, configured_transcription

_LOGGER = logging.getLogger(__name__)


def build_transcript_service(
    runtime: RelayRuntime,
    *,
    config: AutonomyConfig,
    environ: Mapping[str, str] | None = None,
    transport: ModelTransport | None = None,
    transcription: TranscriptionTransport | None = None,
) -> TranscriptService:
    """Bind the selected speech provider and grounded compiler to this relay process."""
    values = os.environ if environ is None else environ
    if transcription is None:
        transcription = configured_transcription(values)
    if transport is None:
        api_key = values.get("ANTHROPIC_API_KEY")
        if not api_key:
            _LOGGER.warning(
                "ANTHROPIC_API_KEY is not set: transcripts return compiler_unavailable and "
                "the console compiles them with its labelled local fallback"
            )
            return TranscriptService(transcription=transcription)
        transport = AnthropicTransport(api_key=api_key)
    capability_profile = config.effective_capability_profile(runtime.settings.capability_profile)
    qualified_voice_intents = _qualified_voice_intents(values, capability_profile)
    if not qualified_voice_intents:
        _LOGGER.warning(
            "no speech/intent pairs are qualified: proposed plans will be refused "
            "capability_unavailable"
        )

    def navigation(_relay_state: Mapping[str, object]) -> NavigationGrounding | None:
        deployment = config.navigation_deployment
        if deployment is None:
            return None
        metadata = navigation_metadata(deployment.runtime)
        return navigation_from_record(
            {**metadata, **capability_profile.state_value()}, capability_profile
        )

    compiler = RelayTranscriptCompiler(
        sessions=runtime.sessions.get,
        transport=transport,
        translation_policy=TranslationPolicy(
            frame=config.planning.translation_frame,
            step_m=config.planning.translation_step_m,
        ),
        navigation=navigation,
        capability_profile=capability_profile,
        qualified_voice_intents=qualified_voice_intents,
    )
    return TranscriptService(transcription=transcription, compiler=compiler)


def _qualified_voice_intents(
    environ: Mapping[str, str], capability_profile: CapabilityProfile
) -> tuple[str, ...]:
    """Parse the deployment's measured speech/intent qualification allowlist.

    Empty is the fail-closed default.  The setting enables only pairs whose
    external acceptance evidence has passed; it cannot widen the relay's
    immutable capability profile or name a non-Intent-v1 operation.
    """
    raw = environ.get("SWEEP_QUALIFIED_VOICE_INTENTS", "")
    if not raw.strip():
        return ()
    values = tuple(part.strip() for part in raw.split(","))
    known = {name.value for name in IntentName}
    if (
        any(not value for value in values)
        or len(set(values)) != len(values)
        or any(value not in known for value in values)
        or any(not capability_profile.supports(IntentName(value)) for value in values)
    ):
        raise SettingsError(
            "SWEEP_QUALIFIED_VOICE_INTENTS must contain unique enabled Intent v1 names"
        )
    return tuple(sorted(values))


def transcript_service_factory(
    config: AutonomyConfig, environ: Mapping[str, str] | None = None
) -> Callable[[RelayRuntime], TranscriptService]:
    """The ``create_app`` hook that binds the compiler to the started runtime."""

    def factory(runtime: RelayRuntime) -> TranscriptService:
        return build_transcript_service(runtime, config=config, environ=environ)

    return factory


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
        app, composition = create_autonomy_app(
            settings, config, transcript_service_factory=transcript_service_factory(config)
        )
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
