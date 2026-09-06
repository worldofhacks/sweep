"""Language integration on the isolated fleet, with explicit synthetic test inputs.

The default uses the normal Anthropic compiler and Whisper transcription transports.
``--synthetic-inputs`` substitutes bounded phrase fixtures and queued transcripts;
these are synthetic software checks, not recorded provider or recognition evidence.
Audio still passes through the production upload and duration validation, and model
proposals still pass through the normal grounded compiler and confirmation path.
"""

from __future__ import annotations

import argparse
import hmac
import json
import signal
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from queue import Empty, Full, Queue

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from adapters.sim.demo import DemoConfig, FleetDemo, demo_autonomy_config
from language.telemetry import NoOpTraceSink
from language.transport import (
    PINNED_COMPILER_MODEL,
    PROMPT_SCHEMA_VERSION,
    ModelRequest,
    ModelResponse,
)
from relay.autonomy import AutonomyComposition
from relay.language_runtime import LanguageRuntime
from relay.voice import AudioUpload, TranscriptionError, TranscriptService
from relay.voice_telemetry import NoOpVoiceTraceSink


class SyntheticPhraseTransport:
    """A small exact-phrase fixture set using the current authoritative targets."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        text = " ".join(request.transcript.casefold().strip().split()).rstrip(".!?")
        selection = list(request.facts["selection"])
        args: dict[str, object] = {}
        names = {
            "arm": "arm",
            "arm the fleet": "arm",
            "take off": "takeoff",
            "takeoff": "takeoff",
            "hold": "hold",
            "hold position": "hold",
            "stop": "hold",
            "come home": "come_home",
            "return home": "come_home",
            "land": "land",
            "land selected drones": "land",
            "land all": "land_all",
            "land all drones": "land_all",
            "emergency stop": "estop",
            "network stop": "estop",
        }
        name = names.get(text)
        if name in {"arm", "land_all", "estop"}:
            selection = []
        ready_ids = [drone["drone_id"] for drone in request.facts["drones"] if drone["selectable"]]
        selections = {
            "select all drones": ready_ids,
            "select drones 1 and 2": [1, 2],
            "select drones 3 and 4": [3, 4],
            **{f"select drone {drone_id}": [drone_id] for drone_id in range(1, 5)},
        }
        if text in selections:
            name = "select"
            selection = list(selections[text])
            args = {"ids": selection}
        directions = {
            "move forward 0.5 meters": (0.0, 1.0),
            "move backward 0.5 meters": (0.0, -1.0),
            "move left 0.5 meters": (-1.0, 0.0),
            "move right 0.5 meters": (1.0, 0.0),
        }
        if text in directions:
            name = "translate"
            dx, dy = directions[text]
            step = request.facts["translation"]["step_m"]
            args = {"dx": dx * 0.5 / step, "dy": dy * 0.5 / step}
        payload = (
            {"kind": "refuse"}
            if name is None
            else {
                "kind": "plan",
                "intents": [{"name": name, "args": args, "selection": selection, "mode": "indoor"}],
            }
        )
        return ModelResponse(
            payload=payload,
            source="synthetic",
            origin="synthetic",
            model=PINNED_COMPILER_MODEL,
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
        )


class SyntheticTranscriptionTransport:
    """One queued transcript per validated audio upload; no audio recognition claim."""

    def __init__(self) -> None:
        self._pending: Queue[str] = Queue(maxsize=1)

    def queue(self, text: str) -> None:
        self._pending.put_nowait(text)

    def transcribe(self, _upload: AudioUpload) -> str:
        try:
            return self._pending.get_nowait()
        except Empty:
            raise TranscriptionError("no synthetic transcript is queued") from None


class _TranscriptInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=4_000)


class _SyntheticTranscriptService(TranscriptService):
    def process(self, **kwargs: object):
        outcome = super().process(**kwargs)
        # The production voice contract supports whisper/template origins. Keep
        # queued text labeled template, while compilation reports synthetic.
        return replace(outcome, source="template")


def language_demo(config: DemoConfig | None = None, *, synthetic_inputs: bool = False) -> FleetDemo:
    """Create a fleet with language enabled and no navigation/hardware artifacts."""
    language = (
        LanguageRuntime(SyntheticPhraseTransport(), tracer=NoOpTraceSink())
        if synthetic_inputs
        else LanguageRuntime()
    )
    autonomy = replace(demo_autonomy_config(), language=language)
    transcription = SyntheticTranscriptionTransport()

    def configure_app(app: FastAPI, composition: AutonomyComposition) -> None:
        class Compiler:
            def compile(self, text: str, _state: object, **kwargs: object):
                return (
                    composition.session(kwargs["session_id"]).compile_text(
                        text, kwargs["correlation_id"]
                    ),
                    None,
                )

        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application: FastAPI) -> AsyncIterator[None]:
            async with original_lifespan(application):
                application.state.transcript_service = (
                    _SyntheticTranscriptService(
                        transcription=transcription,
                        compiler=Compiler(),
                        tracer=NoOpVoiceTraceSink(),
                    )
                    if synthetic_inputs
                    else TranscriptService(compiler=Compiler())
                )
                yield

        app.router.lifespan_context = lifespan

        if not synthetic_inputs:
            return

        @app.post("/demo/language/next")
        def queue_transcript(
            body: _TranscriptInput, authorization: str | None = Header(default=None)
        ) -> dict[str, str]:
            if authorization is None or not hmac.compare_digest(
                authorization, f"Bearer {demo.token}"
            ):
                raise HTTPException(status_code=401, detail="demo authentication required")
            try:
                transcription.queue(body.text)
            except Full:
                raise HTTPException(
                    status_code=409, detail="a synthetic transcript is pending"
                ) from None
            return {"status": "queued", "source": "synthetic"}

        # FleetDemo may mount the built console at /. Keep this API ahead of
        # that catch-all route, while leaving the production routes untouched.
        app.router.routes.insert(0, app.router.routes.pop())

    demo = FleetDemo(config, autonomy_config=autonomy, configure_app=configure_app)
    return demo


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--session")
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--console-dist", type=Path)
    parser.add_argument("--synthetic-inputs", action="store_true")
    args = parser.parse_args(argv)
    values = vars(args).copy()
    synthetic = values.pop("synthetic_inputs")
    if values["session"] is None:
        del values["session"]
    config = DemoConfig(**values)
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    with language_demo(config, synthetic_inputs=synthetic) as demo:
        print(
            json.dumps(
                {
                    "type": "demo.ready",
                    **demo.status(),
                    "language_mode": "synthetic" if synthetic else "live_providers",
                }
            ),
            flush=True,
        )
        stopped.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
