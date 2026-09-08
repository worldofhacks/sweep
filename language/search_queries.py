"""Resolve supported search phrases against the configured detector and areas."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from typing import Literal

from language.telemetry import TraceSink, get_default_trace_sink


@dataclass(frozen=True, slots=True)
class SearchQueryFacts:
    zones: tuple[str, ...]
    target_classes: tuple[str, ...]

    def __post_init__(self) -> None:
        for values in (self.zones, self.target_classes):
            if (
                not isinstance(values, tuple)
                or not 1 <= len(values) <= 64
                or len(set(values)) != len(values)
                or any(
                    not isinstance(value, str) or not value or len(value) > 128 for value in values
                )
            ):
                raise ValueError("search facts require bounded unique identifiers")


@dataclass(frozen=True, slots=True)
class SearchQueryResolution:
    status: Literal["resolved", "clarify"]
    detail: str
    zone_id: str | None = None
    target_class: str | None = None
    mode: Literal["search", "survey"] = "search"
    source: Literal["template"] = "template"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def resolve_search_query(
    query: str,
    facts: SearchQueryFacts,
    *,
    session_id: str,
    correlation_id: str,
    tracer: TraceSink | None = None,
) -> SearchQueryResolution:
    """Resolve without dispatch; movement still requires a route preview and confirmation."""
    sink = tracer if tracer is not None else get_default_trace_sink()
    started = time.monotonic()
    facts_digest = hashlib.sha256(json.dumps(asdict(facts), sort_keys=True).encode()).hexdigest()
    trace = {
        "correlation_id": correlation_id,
        "session_id": session_id,
        "model": "template",
        "state_digest": facts_digest,
        "prompt_schema_version": "search-query-v1",
    }
    _trace(sink, {**trace, "event": "compiler_started"})
    result = _resolve(query, facts)
    _trace(
        sink,
        {
            **trace,
            "event": "compiler_completed",
            "outcome": result.status,
            "source": result.source,
            "origin": "template",
            "grounded": int(result.status == "resolved"),
            "reason": None if result.status == "resolved" else result.detail,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "input_units": 0,
            "output_units": 0,
        },
    )
    return result


def _resolve(query: str, facts: SearchQueryFacts) -> SearchQueryResolution:
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        return SearchQueryResolution("clarify", "Enter a search request of 1–2000 characters.")
    text = _words(query).removeprefix("please ").rstrip(".?!")
    if re.search(r"\b(?:not|never|cannot|don't|do not|stop|cancel)\b", text):
        return SearchQueryResolution(
            "clarify", "This request stops or negates a search. No search was prepared."
        )
    survey = re.fullmatch(r"survey (?:the )?(.+?)(?: grid)?", text)
    if survey is not None:
        zone = survey[1]
        zones = [value for value in facts.zones if _words(value) == _words(zone)]
        if len(zones) != 1:
            return SearchQueryResolution("clarify", "Choose one of the configured survey rooms.")
        return SearchQueryResolution(
            "resolved",
            "The room is configured. Preview the coverage route before confirming.",
            zone_id=zones[0],
            mode="survey",
        )

    match = re.fullmatch(r"(?:find|look for|search for) (.+?) (?:in|inside) (.+)", text)
    if match is None:
        reverse = re.fullmatch(r"search (.+?) for (.+)", text)
        if reverse is not None:
            target, zone = reverse[2], reverse[1]
        else:
            match = re.fullmatch(r"(?:find|look for|search for) (.+)", text)
            if match is None or len(facts.zones) != 1:
                return SearchQueryResolution(
                    "clarify",
                    "Name one target and room, for example: find a backpack in the lobby.",
                )
            target, zone = match[1], facts.zones[0]
    else:
        target, zone = match[1], match[2]
    target = re.sub(r"^(?:a|an|the) ", "", target)
    zone = zone.removeprefix("the ")
    zones = [value for value in facts.zones if _words(value) == _words(zone)]
    labels = [value for value in facts.target_classes if target in _label_words(value)]
    if len(zones) != 1:
        return SearchQueryResolution("clarify", "Choose one of the configured search rooms.")
    if len(labels) != 1:
        return SearchQueryResolution(
            "clarify",
            "Choose a supported object class. "
            "Colour, ownership, and appearance filters are unavailable.",
        )
    return SearchQueryResolution(
        "resolved",
        "The target and room are configured. Preview the route before confirming.",
        zone_id=zones[0],
        target_class=labels[0],
    )


def _words(text: str) -> str:
    return " ".join(text.casefold().replace("_", " ").replace("-", " ").split())


def _label_words(label: str) -> set[str]:
    value = _words(label)
    return {value, value + "s", "people" if value == "person" else value}


def _trace(sink: TraceSink, event: dict[str, object]) -> None:
    try:
        sink.record(event)
    except Exception:
        pass
