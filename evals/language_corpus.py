from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from types import MappingProxyType
from weakref import ReferenceType, WeakKeyDictionary, ref

from language.compiler import InMemoryAuditSink, TranscriptCompiler
from language.contracts import OutcomeKind
from language.transport import (
    PINNED_COMPILER_MODEL,
    PROMPT_SCHEMA_VERSION,
    AnthropicTransport,
    ModelResponse,
    ModelTransport,
    RecordingTransport,
    ReplayTransport,
)
from planner.models import TranslationGrounding, TranslationPolicy

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
REVIEWED_CORPUS_DIGEST = "4e85885366ff4c3762e86c59e822ab3ebcb1f169da898e0ffe5a3b1912710cbb"
REVIEWED_CORPUS_CASES = 53
_HOST_MINTED_EXPECTATIONS = {
    "capture-explicit-living-room": "capture-living-room-4",
    "capture-explicit-reconstruct-eight": "capture-living-room-5",
}


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
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


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
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


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
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


@dataclass(frozen=True, slots=True)
class _EvaluationEvidence:
    case: CorpusCase
    result: tuple[object, ...]


_ISSUED_CORPORA: WeakKeyDictionary[LoadedCorpus, tuple[CorpusCase, ...]] = WeakKeyDictionary()
_ISSUED_CASES: WeakKeyDictionary[CorpusCase, ReferenceType[LoadedCorpus]] = WeakKeyDictionary()
_ISSUED_RESULTS: WeakKeyDictionary[CaseResult, _EvaluationEvidence] = WeakKeyDictionary()


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
        raise ValueError("default language corpus does not match the reviewed 53-case release")
    loaded = LoadedCorpus(cases=parsed, digest=corpus_digest, reviewed=reviewed)
    if reviewed:
        _ISSUED_CORPORA[loaded] = parsed
        for case in parsed:
            _ISSUED_CASES[case] = ref(loaded)
    return loaded


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
            else TranslationGrounding(
                policy=TranslationPolicy(
                    frame=case.translation_frame,
                    step_m=case.translation_step_m,
                ),
                headings={
                    drone["drone_id"]: drone["heading_deg"]
                    for drone in case.relay_state["drones"]
                    if "heading_deg" in drone
                },
            )
        ),
        qualified_voice_intents=case.qualified_voice_intents,
        now_ms=case.now_ms,
        correlation_id=case.case_id,
    )
    actual_intents = tuple(_freeze_json(intent.semantic_dict()) for intent in outcome.intents)
    expected_kind = case.expected["kind"]
    expected_reason = case.expected.get("reason")
    expected_intents = _expected_intents(
        case.expected.get("intents", []), actual_intents, case_id=case.case_id
    )
    expected_detail = case.expected.get("detail")
    expected_pending_intent_id = case.expected.get("pending_intent_id")
    passed = (
        outcome.kind.value == expected_kind
        and (None if outcome.reason is None else outcome.reason.value) == expected_reason
        and _thaw_json(actual_intents) == expected_intents
        and ("detail" not in case.expected or outcome.detail == expected_detail)
        and outcome.pending_intent_id == expected_pending_intent_id
    )
    source, origin = _trusted_provenance(outcome.source, trace.completed, transport)
    model = str(trace.completed.get("model", PINNED_COMPILER_MODEL))
    prompt_schema_version = str(trace.completed.get("prompt_schema_version", PROMPT_SCHEMA_VERSION))
    cassette_digest = _optional_digest(trace.completed.get("cassette_digest"))
    result = CaseResult(
        case_id=case.case_id,
        passed=passed,
        actual_kind=outcome.kind.value,
        actual_reason=None if outcome.reason is None else outcome.reason.value,
        actual_detail=outcome.detail,
        actual_pending_intent_id=outcome.pending_intent_id,
        actual_intents=actual_intents,
        source=source,
        origin=origin,
        model=model,
        prompt_schema_version=prompt_schema_version,
        cassette_digest=cassette_digest,
        category=case.category,
        live_demo=case.live_demo,
        corpus_digest=case.corpus_digest,
        input_units=_metric(trace.completed.get("input_units")),
        output_units=_metric(trace.completed.get("output_units")),
        latency_ms=_metric(trace.completed.get("provider_latency_ms")),
    )
    _ISSUED_RESULTS[result] = _EvaluationEvidence(case=case, result=_result_evidence(result))
    return result


def append_jsonl_run(
    results: Sequence[CaseResult],
    path: Path,
    *,
    run_id: str,
    corpus: LoadedCorpus,
) -> None:
    if not run_id:
        raise ValueError("run ID must be non-empty")
    materialized = tuple(results)
    regraded = _validate_and_regrade(materialized, corpus)
    path.parent.mkdir(parents=True, exist_ok=True)
    categories: dict[str, int] = {}
    for result in materialized:
        categories[result.category] = categories.get(result.category, 0) + 1
    manifest = {
        "type": "manifest",
        "run_id": run_id,
        "cases": len(materialized),
        "case_ids": list(corpus.case_ids),
        "passed": sum(regraded),
        "corpus_digest": corpus.digest,
        "models": sorted({result.model for result in materialized}),
        "prompt_schema_versions": sorted({result.prompt_schema_version for result in materialized}),
        "response_sources": sorted({result.source for result in materialized}),
        "response_origins": sorted({result.origin for result in materialized}),
        "cassette_digests": sorted(
            {result.cassette_digest for result in materialized if result.cassette_digest}
        ),
        "categories": categories,
        "live_demo_cases": sum(result.live_demo for result in materialized),
    }
    rows = [json.dumps(manifest, separators=(",", ":"), sort_keys=True)]
    for result, passed in zip(materialized, regraded, strict=True):
        rows.append(
            json.dumps(
                {
                    "type": "case",
                    "run_id": run_id,
                    "case_id": result.case_id,
                    "passed": passed,
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
                    "actual_intents": _thaw_json(result.actual_intents),
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


def write_dashboard(
    results: Sequence[CaseResult], path: Path, *, run_id: str, corpus: LoadedCorpus
) -> None:
    materialized = tuple(results)
    regraded = _validate_and_regrade(materialized, corpus)
    passed_count = sum(regraded)
    rows = "".join(
        "<tr>"
        f"<td>{escape(result.case_id)}</td>"
        f"<td>{'pass' if passed else 'fail'}</td>"
        f"<td>{escape(result.actual_kind)}</td>"
        f"<td>{escape(result.actual_reason or '')}</td>"
        f"<td>{result.input_units + result.output_units}</td>"
        f"<td>{result.latency_ms}</td>"
        "</tr>"
        for result, passed in zip(materialized, regraded, strict=True)
    )
    document = (
        '<!doctype html><meta charset="utf-8">'
        f"<title>Language eval {escape(run_id)}</title>"
        f"<h1>Language eval {escape(run_id)}</h1>"
        f"<p>{passed_count}/{len(materialized)} cases passed.</p>"
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


def _expected_intents(
    expected: object,
    actual: Sequence[Mapping[str, object]],
    *,
    case_id: str,
) -> object:
    if not isinstance(expected, Sequence) or isinstance(expected, str | bytes):
        return expected
    normalized = _thaw_json(expected)
    assert isinstance(normalized, list)
    for index, intent in enumerate(normalized):
        if (
            index < len(actual)
            and intent.get("name") == "capture_room"
            and isinstance(intent.get("args"), dict)
            and intent["args"].get("capture_id") == _HOST_MINTED_EXPECTATIONS.get(case_id)
        ):
            intent["args"]["capture_id"] = (
                "capture-" + uuid.uuid5(uuid.NAMESPACE_URL, f"sweep:{case_id}:capture:{index}").hex
            )
    return normalized


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_thaw_json(item) for item in value]
    return value


def _result_matches_case(result: CaseResult, case: CorpusCase) -> bool:
    expected_intents = _expected_intents(
        case.expected.get("intents", []), result.actual_intents, case_id=case.case_id
    )
    return (
        result.actual_kind == case.expected["kind"]
        and result.actual_reason == case.expected.get("reason")
        and _thaw_json(result.actual_intents) == expected_intents
        and ("detail" not in case.expected or result.actual_detail == case.expected.get("detail"))
        and result.actual_pending_intent_id == case.expected.get("pending_intent_id")
    )


def _validate_and_regrade(results: Sequence[CaseResult], corpus: LoadedCorpus) -> tuple[bool, ...]:
    if not isinstance(corpus, LoadedCorpus):
        raise ValueError("eval output requires a loaded corpus")
    issued_cases = _ISSUED_CORPORA.get(corpus)
    if (
        not corpus.reviewed
        or corpus.digest != REVIEWED_CORPUS_DIGEST
        or issued_cases is None
        or corpus.cases is not issued_cases
    ):
        raise ValueError("eval output requires the reviewed loaded corpus")
    if tuple(result.case_id for result in results) != corpus.case_ids:
        raise ValueError("eval results must cover every case exactly once in corpus order")
    if any(
        (evidence := _ISSUED_RESULTS.get(result)) is None
        or evidence.case is not case
        or (owner := _ISSUED_CASES.get(case)) is None
        or owner() is not corpus
        or evidence.result != _result_evidence(result)
        for result, case in zip(results, issued_cases, strict=True)
    ):
        raise ValueError("eval results must be issued by evaluation for the loaded corpus")
    return tuple(
        _result_matches_case(result, case)
        for result, case in zip(results, issued_cases, strict=True)
    )


def _trusted_provenance(
    outcome_source: str,
    trace: Mapping[str, object],
    transport: ModelTransport,
) -> tuple[str, str]:
    if outcome_source == "template":
        return "template", "template"
    if type(transport) is StaticResponseTransport:
        expected = ("synthetic", "synthetic")
    elif type(transport) is ReplayTransport:
        expected = ("replay", "unverified_replay")
    elif type(transport) is AnthropicTransport:
        expected = ("anthropic", "anthropic")
    elif type(transport) is RecordingTransport:
        expected = (transport.recorded_origin, transport.recorded_origin)
    else:
        raise ValueError("evaluation transport provenance is not trusted")
    actual = (outcome_source, str(trace.get("origin", "")))
    if actual != expected:
        raise ValueError("evaluation response provenance does not match its transport")
    return expected


def _result_evidence(result: CaseResult) -> tuple[object, ...]:
    return (
        result.case_id,
        result.passed,
        result.actual_kind,
        result.actual_reason,
        result.actual_detail,
        result.actual_pending_intent_id,
        result.actual_intents,
        result.source,
        result.origin,
        result.model,
        result.prompt_schema_version,
        result.cassette_digest,
        result.category,
        result.live_demo,
        result.corpus_digest,
        result.input_units,
        result.output_units,
        result.latency_ms,
    )
