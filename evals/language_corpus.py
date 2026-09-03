from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path

from language.compiler import InMemoryAuditSink, TranscriptCompiler
from language.contracts import OutcomeKind
from language.transport import ModelResponse, ModelTransport

DEFAULT_CORPUS_PATH = (
    Path(__file__).resolve().parent.parent
    / "datasets"
    / "utterances"
    / "transcript_plan_cases.json"
)
DEFAULT_SYNTHETIC_RESPONSES_PATH = (
    Path(__file__).resolve().parent.parent
    / "datasets"
    / "utterances"
    / "transcript_plan_responses.synthetic.json"
)


@dataclass(frozen=True, slots=True)
class CorpusCase:
    case_id: str
    transcript: str
    relay_state: Mapping[str, object]
    capability_version: str
    rooms: tuple[str, ...]
    now_ms: int
    expected: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    passed: bool
    actual_kind: str
    actual_reason: str | None
    actual_intents: tuple[Mapping[str, object], ...]
    source: str
    input_units: int
    output_units: int
    latency_ms: int


class EvalTrace:
    def __init__(self) -> None:
        self.completed: Mapping[str, object] = {}

    def record(self, event: Mapping[str, object]) -> None:
        if event.get("event") == "compiler_completed":
            self.completed = dict(event)


class StaticResponseTransport(ModelTransport):
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def complete(self, request: object) -> ModelResponse:
        return ModelResponse(payload=self._payload)


def load_corpus(path: Path = DEFAULT_CORPUS_PATH) -> tuple[CorpusCase, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load language corpus: {error}") from None
    if (
        not isinstance(raw, Mapping)
        or raw.get("version") != 1
        or set(raw)
        != {
            "version",
            "cases",
        }
    ):
        raise ValueError("language corpus has an unsupported schema")
    cases = raw["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("language corpus requires at least one case")
    parsed = tuple(_parse_case(item) for item in cases)
    ids = [case.case_id for case in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError("language corpus case IDs must be unique")
    return parsed


def load_synthetic_responses(
    path: Path = DEFAULT_SYNTHETIC_RESPONSES_PATH,
) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load synthetic responses: {error}") from None
    if (
        not isinstance(raw, Mapping)
        or raw.get("version") != 1
        or set(raw) != {"version", "responses"}
        or not isinstance(raw["responses"], Mapping)
    ):
        raise ValueError("synthetic responses have an unsupported schema")
    if not all(isinstance(key, str) and key for key in raw["responses"]):
        raise ValueError("synthetic response IDs must be non-empty strings")
    return dict(raw["responses"])


def evaluate_case(case: CorpusCase, transport: ModelTransport) -> CaseResult:
    trace = EvalTrace()
    compiler = TranscriptCompiler(transport, audit=InMemoryAuditSink(), tracer=trace)
    outcome, _plan = compiler.compile(
        case.transcript,
        case.relay_state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        now_ms=case.now_ms,
        correlation_id=case.case_id,
    )
    actual_intents = tuple(intent.semantic_dict() for intent in outcome.intents)
    expected_kind = case.expected["kind"]
    expected_reason = case.expected.get("reason")
    expected_intents = case.expected.get("intents", [])
    passed = (
        outcome.kind.value == expected_kind
        and (None if outcome.reason is None else outcome.reason.value) == expected_reason
        and list(actual_intents) == expected_intents
    )
    return CaseResult(
        case_id=case.case_id,
        passed=passed,
        actual_kind=outcome.kind.value,
        actual_reason=None if outcome.reason is None else outcome.reason.value,
        actual_intents=actual_intents,
        source=outcome.source,
        input_units=_metric(trace.completed.get("input_units")),
        output_units=_metric(trace.completed.get("output_units")),
        latency_ms=_metric(trace.completed.get("provider_latency_ms")),
    )


def append_jsonl_run(results: Sequence[CaseResult], path: Path, *, run_id: str) -> None:
    if not run_id:
        raise ValueError("run ID must be non-empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "type": "manifest",
        "run_id": run_id,
        "cases": len(results),
        "passed": sum(result.passed for result in results),
    }
    rows = [json.dumps(manifest, separators=(",", ":"), sort_keys=True)]
    for result in results:
        rows.append(
            json.dumps(
                {
                    "type": "case",
                    "run_id": run_id,
                    "case_id": result.case_id,
                    "passed": result.passed,
                    "actual_kind": result.actual_kind,
                    "actual_reason": result.actual_reason,
                    "source": result.source,
                    "input_units": result.input_units,
                    "output_units": result.output_units,
                    "latency_ms": result.latency_ms,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(rows) + "\n")


def write_dashboard(results: Sequence[CaseResult], path: Path, *, run_id: str) -> None:
    passed = sum(result.passed for result in results)
    rows = "".join(
        "<tr>"
        f"<td>{escape(result.case_id)}</td>"
        f"<td>{'pass' if result.passed else 'fail'}</td>"
        f"<td>{escape(result.actual_kind)}</td>"
        f"<td>{escape(result.actual_reason or '')}</td>"
        f"<td>{result.input_units + result.output_units}</td>"
        f"<td>{result.latency_ms}</td>"
        "</tr>"
        for result in results
    )
    document = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>Language eval {escape(run_id)}</title>"
        f"<h1>Language eval {escape(run_id)}</h1>"
        f"<p>{passed}/{len(results)} cases passed.</p>"
        "<table><thead><tr><th>Case</th><th>Result</th><th>Outcome</th>"
        "<th>Reason</th><th>Units</th><th>Latency ms</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def _parse_case(raw: object) -> CorpusCase:
    fields = {
        "id",
        "transcript",
        "relay_state",
        "context",
        "expected",
    }
    required = {"id", "transcript", "relay_state", "context", "expected"}
    if not isinstance(raw, Mapping) or not required <= set(raw) or not set(raw) <= fields:
        raise ValueError("language corpus case fields are invalid")
    case_id = raw["id"]
    transcript = raw["transcript"]
    state = raw["relay_state"]
    context = raw["context"]
    expected = raw["expected"]
    if not isinstance(case_id, str) or not case_id or len(case_id) > 128:
        raise ValueError("case ID must be a bounded non-empty string")
    if not isinstance(transcript, str) or not transcript or len(transcript) > 4_000:
        raise ValueError("case transcript must be a bounded non-empty string")
    if not isinstance(state, Mapping) or not isinstance(context, Mapping):
        raise ValueError("case state and context must be objects")
    if set(context) != {"capability_version", "rooms", "now_ms"}:
        raise ValueError("case context fields are invalid")
    capability_version = context["capability_version"]
    rooms = context["rooms"]
    now_ms = context["now_ms"]
    if not isinstance(capability_version, str) or not capability_version:
        raise ValueError("case capability version is invalid")
    if not _strings(rooms):
        raise ValueError("case rooms must be a string list")
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("case time must be a non-negative integer")
    _validate_expected(expected)
    return CorpusCase(
        case_id=case_id,
        transcript=transcript,
        relay_state=dict(state),
        capability_version=capability_version,
        rooms=tuple(rooms),
        now_ms=now_ms,
        expected=dict(expected),
    )


def _validate_expected(raw: object) -> None:
    if not isinstance(raw, Mapping) or not set(raw) <= {"kind", "intents", "reason"}:
        raise ValueError("expected outcome fields are invalid")
    try:
        kind = OutcomeKind(raw.get("kind"))
    except (TypeError, ValueError):
        raise ValueError("expected outcome kind is invalid") from None
    if kind is OutcomeKind.PLAN:
        intents = raw.get("intents")
        if raw.get("reason") is not None or not isinstance(intents, list) or not intents:
            raise ValueError("plan expectations require intents and no reason")
    elif not isinstance(raw.get("reason"), str) or raw.get("intents") not in (None, []):
        raise ValueError("non-plan expectations require a reason and no intents")


def _strings(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str | bytes)
        and all(isinstance(item, str) and item for item in value)
    )


def _metric(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
