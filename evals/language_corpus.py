from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path

from language.compiler import InMemoryAuditSink, TranscriptCompiler
from language.contracts import OutcomeKind
from language.transport import (
    PINNED_COMPILER_MODEL,
    PROMPT_SCHEMA_VERSION,
    ModelResponse,
    ModelTransport,
)

DEFAULT_CORPUS_PATH = (
    Path(__file__).resolve().parent.parent
    / "datasets"
    / "utterances"
    / "transcript_plan_cases.jsonl"
)
LEGACY_CORPUS_PATH = DEFAULT_CORPUS_PATH.with_suffix(".json")
DEFAULT_SYNTHETIC_RESPONSES_PATH = (
    Path(__file__).resolve().parent.parent
    / "datasets"
    / "utterances"
    / "transcript_plan_responses.synthetic.json"
)
LEGACY_SYNTHETIC_RESPONSES_PATH = (
    Path(__file__).resolve().parent.parent
    / "language"
    / "fixtures"
    / "transcript_plan_responses.synthetic.json"
)
REVIEWED_CORPUS_DIGEST = "d3b4ca32f06c0488fd54bce4f3ae7031d8bb514ff8d4173777915c5111d3ca80"
REVIEWED_CORPUS_CASES = 50
HOST_MINTED_SENTINEL = "__host_minted__"


@dataclass(frozen=True, slots=True)
class CorpusCase:
    case_id: str
    transcript: str
    relay_state: Mapping[str, object]
    capability_version: str
    rooms: tuple[str, ...]
    now_ms: int
    translation_frame: str | None
    translation_step_m: float | None
    qualified_voice_intents: tuple[str, ...]
    expected: Mapping[str, object]
    category: str
    live_demo: bool
    corpus_digest: str


@dataclass(frozen=True, slots=True)
class LoadedCorpus:
    cases: tuple[CorpusCase, ...]
    digest: str
    reviewed: bool

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases)

    def __iter__(self) -> Iterator[CorpusCase]:
        return iter(self.cases)

    def __len__(self) -> int:
        return len(self.cases)

    def __getitem__(self, index: int) -> CorpusCase:
        return self.cases[index]


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    passed: bool
    actual_kind: str
    actual_reason: str | None
    actual_detail: str | None
    actual_pending_intent_id: str | None
    actual_intents: tuple[Mapping[str, object], ...]
    source: str
    origin: str
    model: str
    prompt_schema_version: str
    cassette_digest: str | None
    category: str
    live_demo: bool
    corpus_digest: str
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
        return ModelResponse(
            payload=self._payload,
            source="synthetic",
            origin="synthetic",
            model=PINNED_COMPILER_MODEL,
            prompt_schema_version=PROMPT_SCHEMA_VERSION,
        )


def load_corpus(path: Path | None = None) -> LoadedCorpus:
    selected = path or (DEFAULT_CORPUS_PATH if DEFAULT_CORPUS_PATH.exists() else LEGACY_CORPUS_PATH)
    try:
        corpus_bytes = selected.read_bytes()
        corpus_text = corpus_bytes.decode("utf-8")
        if selected.suffix == ".jsonl":
            cases = [json.loads(line) for line in corpus_text.splitlines() if line.strip()]
        else:
            raw = json.loads(corpus_text)
            if (
                not isinstance(raw, Mapping)
                or raw.get("version") != 1
                or set(raw) != {"version", "cases"}
            ):
                raise ValueError("language corpus has an unsupported schema")
            cases = raw["cases"]
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load language corpus: {error}") from None
    if not isinstance(cases, list) or not cases:
        raise ValueError("language corpus requires at least one case")
    corpus_digest = hashlib.sha256(corpus_bytes).hexdigest()
    parsed = tuple(_parse_case(item, corpus_digest=corpus_digest) for item in cases)
    ids = [case.case_id for case in parsed]
    if len(ids) != len(set(ids)):
        raise ValueError("language corpus case IDs must be unique")
    reviewed = selected.resolve() == DEFAULT_CORPUS_PATH.resolve()
    if reviewed and (
        corpus_digest != REVIEWED_CORPUS_DIGEST or len(parsed) != REVIEWED_CORPUS_CASES
    ):
        raise ValueError("default language corpus does not match the reviewed 50-case release")
    return LoadedCorpus(cases=parsed, digest=corpus_digest, reviewed=reviewed)


def load_synthetic_responses(
    path: Path | None = None,
    *,
    corpus: LoadedCorpus,
) -> Mapping[str, object]:
    if not isinstance(corpus, LoadedCorpus):
        raise ValueError("synthetic responses require a loaded corpus")
    selected = path or (
        DEFAULT_SYNTHETIC_RESPONSES_PATH
        if DEFAULT_SYNTHETIC_RESPONSES_PATH.exists()
        else LEGACY_SYNTHETIC_RESPONSES_PATH
    )
    try:
        raw = json.loads(selected.read_text(encoding="utf-8"))
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
    if tuple(raw["responses"]) != corpus.case_ids:
        raise ValueError("synthetic response IDs must exactly match corpus order")
    return dict(raw["responses"])


def evaluate_case(case: CorpusCase, transport: ModelTransport) -> CaseResult:
    trace = EvalTrace()
    compiler = TranscriptCompiler(transport, audit=InMemoryAuditSink(), tracer=trace)
    outcome, _plan = compiler.compile(
        case.transcript,
        case.relay_state,
        capability_version=case.capability_version,
        rooms=case.rooms,
        translation=(
            None
            if case.translation_frame is None
            else {"frame": case.translation_frame, "step_m": case.translation_step_m}
        ),
        qualified_voice_intents=case.qualified_voice_intents,
        now_ms=case.now_ms,
        correlation_id=case.case_id,
    )
    actual_intents = tuple(_freeze_json(intent.semantic_dict()) for intent in outcome.intents)
    expected_kind = case.expected["kind"]
    expected_reason = case.expected.get("reason")
    expected_intents = _expected_intents(case.expected.get("intents", []), actual_intents)
    expected_detail = case.expected.get("detail")
    expected_pending_intent_id = case.expected.get("pending_intent_id")
    passed = (
        outcome.kind.value == expected_kind
        and (None if outcome.reason is None else outcome.reason.value) == expected_reason
        and list(actual_intents) == expected_intents
        and ("detail" not in case.expected or outcome.detail == expected_detail)
        and outcome.pending_intent_id == expected_pending_intent_id
    )
    return CaseResult(
        case_id=case.case_id,
        passed=passed,
        actual_kind=outcome.kind.value,
        actual_reason=None if outcome.reason is None else outcome.reason.value,
        actual_detail=outcome.detail,
        actual_pending_intent_id=outcome.pending_intent_id,
        actual_intents=actual_intents,
        source=outcome.source,
        origin=str(trace.completed.get("origin", "template")),
        model=str(trace.completed.get("model", PINNED_COMPILER_MODEL)),
        prompt_schema_version=str(
            trace.completed.get("prompt_schema_version", PROMPT_SCHEMA_VERSION)
        ),
        cassette_digest=_optional_digest(trace.completed.get("cassette_digest")),
        category=case.category,
        live_demo=case.live_demo,
        corpus_digest=case.corpus_digest,
        input_units=_metric(trace.completed.get("input_units")),
        output_units=_metric(trace.completed.get("output_units")),
        latency_ms=_metric(trace.completed.get("provider_latency_ms")),
    )


def append_jsonl_run(
    results: Sequence[CaseResult],
    path: Path,
    *,
    run_id: str,
    corpus: LoadedCorpus,
) -> None:
    if not run_id:
        raise ValueError("run ID must be non-empty")
    if not isinstance(corpus, LoadedCorpus):
        raise ValueError("eval manifest requires a loaded corpus")
    if not corpus.reviewed or corpus.digest != REVIEWED_CORPUS_DIGEST:
        raise ValueError("eval manifest requires the reviewed loaded corpus")
    if tuple(result.case_id for result in results) != corpus.case_ids:
        raise ValueError("eval results must cover every case exactly once in corpus order")
    if any(
        result.corpus_digest != corpus.digest
        or result.category != case.category
        or result.live_demo != case.live_demo
        for result, case in zip(results, corpus, strict=True)
    ):
        raise ValueError("eval results do not match the loaded corpus")
    path.parent.mkdir(parents=True, exist_ok=True)
    categories: dict[str, int] = {}
    for result in results:
        categories[result.category] = categories.get(result.category, 0) + 1
    manifest = {
        "type": "manifest",
        "run_id": run_id,
        "cases": len(results),
        "case_ids": list(corpus.case_ids),
        "passed": sum(result.passed for result in results),
        "corpus_digest": corpus.digest,
        "models": sorted({result.model for result in results}),
        "prompt_schema_versions": sorted({result.prompt_schema_version for result in results}),
        "response_sources": sorted({result.source for result in results}),
        "response_origins": sorted({result.origin for result in results}),
        "cassette_digests": sorted(
            {result.cassette_digest for result in results if result.cassette_digest}
        ),
        "categories": categories,
        "live_demo_cases": sum(result.live_demo for result in results),
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
                    "actual_detail": result.actual_detail,
                    "actual_pending_intent_id": result.actual_pending_intent_id,
                    "source": result.source,
                    "origin": result.origin,
                    "model": result.model,
                    "prompt_schema_version": result.prompt_schema_version,
                    "cassette_digest": result.cassette_digest,
                    "category": result.category,
                    "live_demo": result.live_demo,
                    "corpus_digest": result.corpus_digest,
                    "actual_intents": result.actual_intents,
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


def _parse_case(raw: object, *, corpus_digest: str) -> CorpusCase:
    fields = {
        "id",
        "transcript",
        "relay_state",
        "context",
        "expected",
        "category",
        "live_demo",
    }
    required = {"id", "transcript", "relay_state", "context", "expected"}
    if not isinstance(raw, Mapping) or not required <= set(raw) or not set(raw) <= fields:
        raise ValueError("language corpus case fields are invalid")
    case_id = raw["id"]
    transcript = raw["transcript"]
    state = raw["relay_state"]
    context = raw["context"]
    expected = raw["expected"]
    category = raw.get("category", "synthetic")
    live_demo = raw.get("live_demo", False)
    if not isinstance(case_id, str) or not case_id or len(case_id) > 128:
        raise ValueError("case ID must be a bounded non-empty string")
    if not isinstance(transcript, str) or not transcript or len(transcript) > 4_000:
        raise ValueError("case transcript must be a bounded non-empty string")
    if not isinstance(state, Mapping) or not isinstance(context, Mapping):
        raise ValueError("case state and context must be objects")
    if not isinstance(category, str) or not category or not isinstance(live_demo, bool):
        raise ValueError("case category and live-demo marker are invalid")
    if not {"capability_version", "rooms", "now_ms"} <= set(context) or not set(context) <= {
        "capability_version",
        "rooms",
        "now_ms",
        "translation",
        "qualified_voice_intents",
    }:
        raise ValueError("case context fields are invalid")
    capability_version = context["capability_version"]
    rooms = context["rooms"]
    now_ms = context["now_ms"]
    translation = context.get("translation")
    qualified_voice_intents = context.get("qualified_voice_intents", [])
    if not isinstance(capability_version, str) or not capability_version:
        raise ValueError("case capability version is invalid")
    if not _strings(rooms):
        raise ValueError("case rooms must be a string list")
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("case time must be a non-negative integer")
    if translation is not None and (
        not isinstance(translation, Mapping)
        or set(translation) != {"frame", "step_m"}
        or translation["frame"] != "aircraft_relative"
        or not isinstance(translation["step_m"], int | float)
        or isinstance(translation["step_m"], bool)
        or translation["step_m"] <= 0
    ):
        raise ValueError("case translation context is invalid")
    if not _strings(qualified_voice_intents):
        raise ValueError("qualified voice intents must be a string list")
    state = dict(state)
    state.setdefault("v", 1)
    state.setdefault("event_id", f"state-{case_id}")
    state.setdefault("session", "language-eval")
    _validate_expected(expected)
    return CorpusCase(
        case_id=case_id,
        transcript=transcript,
        relay_state=_freeze_json(state),
        capability_version=capability_version,
        rooms=tuple(rooms),
        now_ms=now_ms,
        translation_frame=None if translation is None else "aircraft_relative",
        translation_step_m=None if translation is None else float(translation["step_m"]),
        qualified_voice_intents=tuple(qualified_voice_intents),
        expected=_freeze_json(expected),
        category=category,
        live_demo=live_demo,
        corpus_digest=corpus_digest,
    )


def _validate_expected(raw: object) -> None:
    if not isinstance(raw, Mapping) or not set(raw) <= {
        "kind",
        "intents",
        "reason",
        "detail",
        "pending_intent_id",
    }:
        raise ValueError("expected outcome fields are invalid")
    try:
        kind = OutcomeKind(raw.get("kind"))
    except (TypeError, ValueError):
        raise ValueError("expected outcome kind is invalid") from None
    if kind is OutcomeKind.PLAN:
        intents = raw.get("intents")
        if raw.get("reason") is not None or not isinstance(intents, list) or not intents:
            raise ValueError("plan expectations require intents and no reason")
    elif kind is OutcomeKind.CANCEL_PENDING:
        if (
            set(raw) != {"kind", "pending_intent_id"}
            or not isinstance(raw["pending_intent_id"], str)
            or not raw["pending_intent_id"]
        ):
            raise ValueError("cancel expectations require one pending intent ID")
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


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= set("0123456789abcdef"):
        raise ValueError("cassette digest is invalid")
    return value


def _expected_intents(expected: object, actual: Sequence[Mapping[str, object]]) -> object:
    if not isinstance(expected, list):
        return expected
    normalized = json.loads(json.dumps(expected))
    for index, intent in enumerate(normalized):
        if (
            index < len(actual)
            and intent.get("name") == "capture_room"
            and isinstance(intent.get("args"), dict)
        ):
            actual_args = actual[index].get("args")
            expected_capture_id = intent["args"].get("capture_id")
            actual_capture_id = (
                actual_args.get("capture_id") if isinstance(actual_args, Mapping) else None
            )
            if (
                expected_capture_id == HOST_MINTED_SENTINEL
                and isinstance(actual_capture_id, str)
                and actual_capture_id.startswith("capture-")
                and len(actual_capture_id) == 40
            ):
                intent["args"]["capture_id"] = actual_args["capture_id"]
    return normalized


class _FrozenDict(dict):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("loaded corpus data is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenList(list):
    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("loaded corpus data is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenDict({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return _FrozenList(_freeze_json(item) for item in value)
    return value
