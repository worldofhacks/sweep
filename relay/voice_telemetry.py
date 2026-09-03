"""Credential-guarded telemetry for relay-side voice transcription."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from typing import Protocol


class VoiceTraceSink(Protocol):
    def record(self, event: Mapping[str, object]) -> None: ...


class NoOpVoiceTraceSink:
    def record(self, event: Mapping[str, object]) -> None:
        return None


class LangfuseVoiceTraceSink:
    def __init__(self, client: object, attribute_context: object | None = None) -> None:
        self._client = client
        self._attribute_context = attribute_context
        self._observations: dict[str, object] = {}

    def record(self, event: Mapping[str, object]) -> None:
        correlation_id = event.get("correlation_id")
        if not isinstance(correlation_id, str) or not correlation_id:
            return
        if event.get("event") == "voice_started":
            self._start(correlation_id, event)
            return
        if event.get("event") != "voice_completed":
            return
        observation = self._observations.pop(correlation_id, None)
        if observation is None:
            return
        update = getattr(observation, "update", None)
        if update is not None:
            update(
                output={"status": event.get("status"), "source": event.get("source")},
                metadata={
                    "reason": event.get("reason"),
                    "provider_cost_usd": event.get("provider_cost_usd"),
                    "combined_cost_usd": event.get("combined_cost_usd"),
                },
            )
        score = getattr(observation, "score_trace", None)
        if score is not None:
            score(
                name="transcribed",
                value=1 if event.get("status") == "transcribed" else 0,
                comment=event.get("reason"),
            )
        end = getattr(observation, "end", None)
        if end is not None:
            end()
        flush = getattr(self._client, "flush", None)
        if flush is not None:
            flush()

    def _start(self, correlation_id: str, event: Mapping[str, object]) -> None:
        trace_id_factory = getattr(self._client, "create_trace_id", None)
        observation_factory = getattr(self._client, "start_observation", None)
        if trace_id_factory is None or observation_factory is None:
            return
        context = nullcontext()
        if self._attribute_context is not None:
            context = self._attribute_context(session_id=event.get("session_id"))
        with context:
            self._observations[correlation_id] = observation_factory(
                trace_context={"trace_id": trace_id_factory(seed=correlation_id)},
                name="relay.voice.transcribe",
                as_type="generation",
                model=event.get("model"),
                input={
                    "content_type": event.get("content_type"),
                    "bytes": event.get("bytes"),
                    "audio_duration_ms": event.get("audio_duration_ms"),
                },
                metadata={
                    "correlation_id": correlation_id,
                    "provider_cost_usd": event.get("provider_cost_usd"),
                    "combined_cost_usd": event.get("combined_cost_usd"),
                },
            )


_client: object | None = None
_attribute_context: object | None = None


def get_default_voice_trace_sink(
    environ: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[[str, str, str | None], tuple[object, object | None]] | None = None,
) -> VoiceTraceSink:
    values = os.environ if environ is None else environ
    public_key = values.get("LANGFUSE_PUBLIC_KEY")
    secret_key = values.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        return NoOpVoiceTraceSink()

    global _attribute_context, _client
    if _client is None:
        try:
            if client_factory is not None:
                _client, _attribute_context = client_factory(
                    public_key, secret_key, values.get("LANGFUSE_HOST")
                )
            else:
                from langfuse import Langfuse, propagate_attributes

                _client = Langfuse(
                    public_key=public_key,
                    secret_key=secret_key,
                    host=values.get("LANGFUSE_HOST"),
                )
                _attribute_context = propagate_attributes
        except Exception:
            return NoOpVoiceTraceSink()
    return LangfuseVoiceTraceSink(_client, _attribute_context)
