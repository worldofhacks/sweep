from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Protocol

logger = logging.getLogger(__name__)


class TraceSink(Protocol):
    def record(self, event: Mapping[str, object]) -> None: ...


class NoOpTraceSink:
    def record(self, event: Mapping[str, object]) -> None:
        pass


class LangfuseTraceSink:
    def __init__(self, client: object, attribute_context: object | None = None) -> None:
        self._client = client
        self._attribute_context = attribute_context
        self._observations: dict[str, object] = {}

    def record(self, event: Mapping[str, object]) -> None:
        correlation_id = event.get("correlation_id")
        if not isinstance(correlation_id, str) or not correlation_id:
            return
        if event.get("event") == "compiler_started":
            self._start(correlation_id, event)
            return
        if event.get("event") != "compiler_completed":
            return
        observation = self._observations.pop(correlation_id, None)
        if observation is None:
            return
        update = getattr(observation, "update", None)
        if update is not None:
            update(
                output={"outcome": event.get("outcome"), "source": event.get("source")},
                usage_details={
                    "input": event.get("input_units", 0),
                    "output": event.get("output_units", 0),
                },
                metadata={
                    "provider_latency_ms": event.get("provider_latency_ms", 0),
                    "elapsed_ms": event.get("elapsed_ms", 0),
                    "origin": event.get("origin"),
                    "prompt_schema_version": event.get("prompt_schema_version"),
                    "cassette_digest": event.get("cassette_digest"),
                },
            )
        score = getattr(observation, "score_trace", None)
        if score is not None:
            score(
                name="grounded",
                value=event.get("grounded", 0),
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
        trace_id = trace_id_factory(seed=correlation_id)
        context = nullcontext()
        if self._attribute_context is not None:
            context = self._attribute_context(session_id=event.get("session_id"))
        with context:
            self._observations[correlation_id] = observation_factory(
                trace_context={"trace_id": trace_id},
                name="language.compile",
                as_type="generation",
                model=event.get("model"),
                input={"state_digest": event.get("state_digest")},
                metadata={"correlation_id": correlation_id},
            )


_langfuse_client: object | None = None
_langfuse_attribute_context: object | None = None


def get_default_trace_sink() -> TraceSink:
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        return NoOpTraceSink()

    global _langfuse_attribute_context, _langfuse_client
    if _langfuse_client is not None:
        return LangfuseTraceSink(_langfuse_client, _langfuse_attribute_context)
    try:
        from langfuse import Langfuse, propagate_attributes
    except ImportError:
        logger.warning("Langfuse credentials are configured but the client is unavailable")
        return NoOpTraceSink()
    try:
        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=os.environ.get("LANGFUSE_HOST"),
        )
    except Exception:
        logger.warning("Langfuse client initialization failed; tracing is disabled")
        return NoOpTraceSink()
    _langfuse_attribute_context = propagate_attributes
    return LangfuseTraceSink(_langfuse_client, _langfuse_attribute_context)
