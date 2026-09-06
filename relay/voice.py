"""Bounded audio transcription and compiler handoff at the relay boundary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal, Protocol

import av
import httpx

from arbiter.safety import CONFIRMATION_REQUIRED_INTENTS
from relay.capabilities import IMPLEMENTED_INTENT_NAMES, CapabilityProfile
from relay.intent_v1 import AcceptedIntent, validate_intent
from relay.settings import transcription_provider_from_env
from relay.voice_telemetry import VoiceTraceSink, get_default_voice_trace_sink

WHISPER_MODEL = "whisper-1"
DEEPGRAM_MODEL = "nova-3"
COMMAND_KEYTERMS = (
    "arm",
    "disarm",
    "estop",
    "come home",
    "translate",
    "hold",
    "land",
    "take off",
    "drone one",
    "drone two",
    "drone three",
    "drone four",
    "lobby",
    "kitchen",
    "living room",
    "bedroom",
    "capture room",
)
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_DURATION_MS = 30_000
MAX_TRANSCRIPT_CHARS = 4_000
MAX_TRANSCRIPTION_ATTEMPTS = 2
WHISPER_USD_PER_MINUTE = 0.006
ALLOWED_AUDIO_CONTENT_TYPES = frozenset({"audio/webm", "audio/ogg", "audio/wav", "audio/mpeg"})
_CORRELATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ROOM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEMBERSHIPS = frozenset({"registered", "ready", "leaving", "disconnected", "degraded"})
_FLIGHT_STATES = frozenset(
    {"disarmed", "landed", "armed", "taking_off", "airborne", "hovering", "landing", "emergency"}
)
_CAMERA_PATTERNS = frozenset({"pano_360", "reconstruct_8"})
_MODES = frozenset({"indoor", "outdoor"})
VOICE_PLAN_VERSION = 1
VOICE_PLAN_KINDS = frozenset({"plan", "clarify", "unsupported", "refuse", "cancel_pending"})
MAX_VOICE_PLAN_STEPS = 8
MAX_VOICE_PLAN_OPTIONS = 16
MAX_VOICE_PLAN_NOTES = 8
MAX_VOICE_PLAN_TEXT_CHARS = 500
_INTENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class TranscriptionError(RuntimeError):
    pass


class CompilerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioUpload:
    content_type: str
    body: bytes


class TranscriptionTransport(Protocol):
    def transcribe(self, upload: AudioUpload) -> str: ...


class TranscriptCompiler(Protocol):
    def compile(
        self,
        transcript: str,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...] = (),
        now_ms: int,
        correlation_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[object, object | None]: ...


class UnavailableTranscriptCompiler:
    def compile(
        self,
        transcript: str,
        relay_state: object,
        *,
        capability_version: str,
        rooms: tuple[str, ...] = (),
        now_ms: int,
        correlation_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[object, object | None]:
        del transcript, relay_state, capability_version, rooms, now_ms, correlation_id, session_id
        raise CompilerUnavailable()


class OpenAIWhisperTransport:
    provider = "whisper"
    model = WHISPER_MODEL

    def __init__(self, *, api_key: str | None = None, timeout_s: float = 20.0) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    def transcribe(self, upload: AudioUpload) -> str:
        api_key = self._api_key if self._api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise TranscriptionError("OPENAI_API_KEY is not configured")
        for attempt in range(MAX_TRANSCRIPTION_ATTEMPTS):
            try:
                response = httpx.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": WHISPER_MODEL, "response_format": "json"},
                    files={
                        "file": (
                            "speech" + _extension(upload.content_type),
                            upload.body,
                            upload.content_type,
                        )
                    },
                    timeout=self._timeout_s,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                retryable = status in {408, 409, 429} or status >= 500
                if not retryable or attempt + 1 == MAX_TRANSCRIPTION_ATTEMPTS:
                    raise TranscriptionError("transcription provider request failed") from error
                continue
            except httpx.TransportError as error:
                if attempt + 1 == MAX_TRANSCRIPTION_ATTEMPTS:
                    raise TranscriptionError("transcription provider request failed") from error
                continue
            except httpx.HTTPError as error:
                raise TranscriptionError("transcription provider request failed") from error
            try:
                body = response.json()
            except (TypeError, ValueError) as error:
                raise TranscriptionError("transcription provider response is malformed") from error
            if not isinstance(body, Mapping):
                raise TranscriptionError("transcription provider response is malformed")
            return _validated_transcript(body.get("text"))
        raise AssertionError("bounded transcription attempts must return or raise")


class DeepgramTransport:
    provider = "deepgram"
    model = DEEPGRAM_MODEL

    def __init__(self, *, api_key: str | None = None, timeout_s: float = 20.0) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    def transcribe(self, upload: AudioUpload) -> str:
        api_key = self._api_key if self._api_key is not None else os.environ.get("DEEPGRAM_API_KEY")
        if not api_key:
            raise TranscriptionError("DEEPGRAM_API_KEY is not configured")
        try:
            response = httpx.post(
                "https://api.deepgram.com/v1/listen",
                headers={"Authorization": f"Token {api_key}", "Content-Type": upload.content_type},
                params=[
                    ("model", DEEPGRAM_MODEL),
                    ("language", "en"),
                    ("smart_format", "true"),
                    *(("keyterm", term) for term in COMMAND_KEYTERMS),
                ],
                content=upload.body,
                timeout=self._timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise TranscriptionError("transcription provider request failed") from error
        try:
            text = response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (TypeError, ValueError, KeyError, IndexError) as error:
            raise TranscriptionError("transcription provider response is malformed") from error
        return _validated_transcript(text)


def configured_transcription(
    environ: Mapping[str, str] | None = None, *, provider: str | None = None
) -> TranscriptionTransport:
    values = os.environ if environ is None else environ
    selected = transcription_provider_from_env(values) if provider is None else provider
    if selected == "deepgram":
        return DeepgramTransport(api_key=values.get("DEEPGRAM_API_KEY", ""))
    if selected == "whisper":
        return OpenAIWhisperTransport(api_key=values.get("OPENAI_API_KEY", ""))
    raise ValueError("unknown transcription provider")


class ReplayTranscriptionTransport:
    def __init__(self, cassette_path: Path, *, provider: str = "whisper") -> None:
        if provider not in {"whisper", "deepgram"}:
            raise ValueError("unknown transcription provider")
        self.provider = provider
        self.model = DEEPGRAM_MODEL if provider == "deepgram" else WHISPER_MODEL
        try:
            cassette = json.loads(cassette_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TranscriptionError(f"cannot load replay cassette: {error}") from None
        if not isinstance(cassette, Mapping) or cassette.get("version") != 1:
            raise TranscriptionError("replay cassette has an unsupported schema")
        entries = cassette.get("entries")
        if not isinstance(entries, Mapping):
            raise TranscriptionError("replay cassette entries must be an object")
        self._entries = entries

    def transcribe(self, upload: AudioUpload) -> str:
        entry = self._entries.get(transcription_request_key(upload, model=self.model))
        if not isinstance(entry, Mapping) or set(entry) != {"text"}:
            raise TranscriptionError(
                f"replay miss for {transcription_request_key(upload, model=self.model)}"
            )
        return _validated_transcript(entry.get("text"))


class RecordingTranscriptionTransport:
    def __init__(self, transport: TranscriptionTransport, cassette_path: Path) -> None:
        self._transport = transport
        self.provider = getattr(transport, "provider", "whisper")
        self.model = getattr(transport, "model", WHISPER_MODEL)
        self._cassette_path = cassette_path

    def transcribe(self, upload: AudioUpload) -> str:
        transcript = self._transport.transcribe(upload)
        cassette = self._load()
        entries = cassette["entries"]
        assert isinstance(entries, dict)
        entries[transcription_request_key(upload, model=self.model)] = {"text": transcript}
        self._cassette_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cassette_path.with_suffix(self._cassette_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(cassette, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(self._cassette_path)
        return transcript

    def _load(self) -> dict[str, object]:
        if not self._cassette_path.exists():
            return {"version": 1, "entries": {}}
        try:
            cassette = json.loads(self._cassette_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TranscriptionError(f"cannot load recording cassette: {error}") from None
        if (
            not isinstance(cassette, dict)
            or cassette.get("version") != 1
            or not isinstance(cassette.get("entries"), dict)
        ):
            raise TranscriptionError("recording cassette has an unsupported schema")
        return cassette


@dataclass(frozen=True, slots=True)
class VoicePlanStep:
    """One Intent v1 draft the compiler proposes; the console builds the envelope."""

    index: int
    intent_id: str
    name: str
    args: Mapping[str, object]
    selection: tuple[int, ...]
    mode: str
    confirm_required: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "intent_id": self.intent_id,
            "name": self.name,
            "args": _thaw(self.args),
            "selection": list(self.selection),
            "mode": self.mode,
            "confirm_required": self.confirm_required,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class VoicePlan:
    """The compiler's validated preview: never an emitted intent.

    ``kind`` ``plan`` carries ordered drafts the console stages one at a time after
    operator confirmation; ``clarify`` carries options and emits nothing; ``refuse``
    and ``unsupported`` carry a typed reason; ``cancel_pending`` names the pending
    intent the operator may cancel. Every plan is bound to the relay state event it
    was grounded on and a compiled plan expires at ``expires_at_ms``.
    """

    kind: str
    transcript: str
    reason: str | None
    detail: str | None
    options: tuple[str, ...]
    steps: tuple[VoicePlanStep, ...]
    compiled_at_ms: int
    expires_at_ms: int | None
    state_event_id: str
    roster_version: int
    session: str
    correlation_id: str
    plan_digest: str | None
    model: str
    prompt_schema_version: str
    response_source: str
    pending_intent_id: str | None = None

    def __post_init__(self) -> None:
        _validate_voice_plan_fields(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "v": VOICE_PLAN_VERSION,
            "kind": self.kind,
            "transcript": self.transcript,
            "reason": self.reason,
            "detail": self.detail,
            "options": list(self.options),
            "steps": [step.to_dict() for step in self.steps],
            "compiled_at_ms": self.compiled_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "state_event_id": self.state_event_id,
            "roster_version": self.roster_version,
            "session": self.session,
            "correlation_id": self.correlation_id,
            "plan_digest": self.plan_digest,
            "model": self.model,
            "prompt_schema_version": self.prompt_schema_version,
            "response_source": self.response_source,
            "pending_intent_id": self.pending_intent_id,
        }


@dataclass(frozen=True, slots=True)
class VoiceOutcome:
    status: Literal["transcribed", "refused"]
    source: Literal["whisper", "deepgram", "template"]
    reason: str | None
    transcript: str | None
    emissions: tuple[()] = ()
    plan: VoicePlan | None = None

    def to_dict(self, *, session_id: str, correlation_id: str) -> dict[str, object]:
        return {
            "v": 1,
            "type": "voice_outcome",
            "session": session_id,
            "correlation_id": correlation_id,
            "status": self.status,
            "source": self.source,
            "reason": self.reason,
            "transcript": self.transcript,
            "emissions": [],
            "plan": None if self.plan is None else self.plan.to_dict(),
        }


_VOICE_OUTCOME_FIELDS = frozenset(
    {
        "v",
        "type",
        "session",
        "correlation_id",
        "status",
        "source",
        "reason",
        "transcript",
        "emissions",
        "plan",
    }
)
_VOICE_PLAN_FIELDS = frozenset(
    {
        "v",
        "kind",
        "transcript",
        "reason",
        "detail",
        "options",
        "steps",
        "compiled_at_ms",
        "expires_at_ms",
        "state_event_id",
        "roster_version",
        "session",
        "correlation_id",
        "plan_digest",
        "model",
        "prompt_schema_version",
        "response_source",
        "pending_intent_id",
    }
)
_VOICE_PLAN_STEP_FIELDS = frozenset(
    {
        "index",
        "intent_id",
        "name",
        "args",
        "selection",
        "mode",
        "confirm_required",
        "notes",
    }
)


def parse_voice_outcome(
    raw: object, *, session_id: str | None = None, correlation_id: str | None = None
) -> VoiceOutcome:
    """Validate a ``voice_outcome`` wire object; the console mirror applies the same rules."""
    if not isinstance(raw, Mapping) or set(raw) != _VOICE_OUTCOME_FIELDS:
        raise ValueError("voice outcome has unexpected fields")
    if raw["v"] != 1 or raw["type"] != "voice_outcome":
        raise ValueError("voice outcome version or type is unsupported")
    if session_id is not None and raw["session"] != session_id:
        raise ValueError("voice outcome session does not match")
    if correlation_id is not None and raw["correlation_id"] != correlation_id:
        raise ValueError("voice outcome correlation does not match")
    if raw["status"] not in {"transcribed", "refused"} or raw["source"] not in {
        "whisper",
        "template",
    }:
        raise ValueError("voice outcome status or source is invalid")
    if raw["reason"] is not None and not isinstance(raw["reason"], str):
        raise ValueError("voice outcome reason must be a string or null")
    if raw["transcript"] is not None and not isinstance(raw["transcript"], str):
        raise ValueError("voice outcome transcript must be a string or null")
    if raw["emissions"] != []:
        raise ValueError("voice outcome never carries emissions")
    plan = None if raw["plan"] is None else parse_voice_plan(raw["plan"])
    if plan is not None and (
        raw["status"] != "transcribed"
        or plan.session != raw["session"]
        or plan.correlation_id != raw["correlation_id"]
        or plan.transcript != raw["transcript"]
    ):
        raise ValueError("voice plan must belong to its transcribed outcome")
    return VoiceOutcome(raw["status"], raw["source"], raw["reason"], raw["transcript"], (), plan)


def parse_voice_plan(raw: object) -> VoicePlan:
    """Rebuild a ``VoicePlan`` from its wire object, refusing anything outside the contract."""
    if not isinstance(raw, Mapping) or set(raw) != _VOICE_PLAN_FIELDS:
        raise ValueError("voice plan has unexpected fields")
    if raw["v"] != VOICE_PLAN_VERSION:
        raise ValueError("voice plan version is unsupported")
    options = raw["options"]
    steps = raw["steps"]
    if not isinstance(options, list) or not isinstance(steps, list):
        raise ValueError("voice plan options and steps must be lists")
    return VoicePlan(
        kind=raw["kind"],
        transcript=raw["transcript"],
        reason=raw["reason"],
        detail=raw["detail"],
        options=tuple(options),
        steps=tuple(_parse_voice_plan_step(step) for step in steps),
        compiled_at_ms=raw["compiled_at_ms"],
        expires_at_ms=raw["expires_at_ms"],
        state_event_id=raw["state_event_id"],
        roster_version=raw["roster_version"],
        session=raw["session"],
        correlation_id=raw["correlation_id"],
        plan_digest=raw["plan_digest"],
        model=raw["model"],
        prompt_schema_version=raw["prompt_schema_version"],
        response_source=raw["response_source"],
        pending_intent_id=raw["pending_intent_id"],
    )


def _parse_voice_plan_step(raw: object) -> VoicePlanStep:
    if not isinstance(raw, Mapping) or set(raw) != _VOICE_PLAN_STEP_FIELDS:
        raise ValueError("voice plan step has unexpected fields")
    args = raw["args"]
    selection = raw["selection"]
    notes = raw["notes"]
    if not isinstance(args, Mapping) or not isinstance(selection, list):
        raise ValueError("voice plan step args and selection are invalid")
    if not isinstance(notes, list):
        raise ValueError("voice plan step notes must be a list")
    return VoicePlanStep(
        index=raw["index"],
        intent_id=raw["intent_id"],
        name=raw["name"],
        args=dict(args),
        selection=tuple(selection),
        mode=raw["mode"],
        confirm_required=raw["confirm_required"],
        notes=tuple(notes),
    )


def _bounded_text(value: object, *, limit: int = MAX_VOICE_PLAN_TEXT_CHARS) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= limit
        and all(ord(character) >= 32 for character in value)
    )


def _validate_voice_plan_fields(plan: VoicePlan) -> None:
    if plan.kind not in VOICE_PLAN_KINDS:
        raise ValueError("voice plan kind is unsupported")
    if not _bounded_text(plan.transcript, limit=MAX_TRANSCRIPT_CHARS):
        raise ValueError("voice plan requires the transcript it compiled")
    if not plan.transcript.strip():
        raise ValueError("voice plan requires the transcript it compiled")
    for text_field in ("reason", "detail", "plan_digest", "pending_intent_id"):
        value = getattr(plan, text_field)
        if value is not None and not _bounded_text(value):
            raise ValueError(f"voice plan {text_field} must be a bounded string or null")
    for text_field in (
        "state_event_id",
        "session",
        "correlation_id",
        "model",
        "prompt_schema_version",
        "response_source",
    ):
        if not _bounded_text(getattr(plan, text_field), limit=512):
            raise ValueError(f"voice plan {text_field} must be a bounded string")
    _nonnegative_integer(plan.compiled_at_ms)
    _nonnegative_integer(plan.roster_version)
    if plan.expires_at_ms is not None and (
        _nonnegative_integer(plan.expires_at_ms) <= plan.compiled_at_ms
    ):
        raise ValueError("voice plan must expire after it was compiled")
    if not isinstance(plan.options, tuple) or len(plan.options) > MAX_VOICE_PLAN_OPTIONS:
        raise ValueError("voice plan options must be a bounded tuple")
    if any(not _bounded_text(option) for option in plan.options):
        raise ValueError("voice plan options must be bounded strings")
    if len(set(plan.options)) != len(plan.options):
        raise ValueError("voice plan options must be unique")
    if not isinstance(plan.steps, tuple) or len(plan.steps) > MAX_VOICE_PLAN_STEPS:
        raise ValueError("voice plan carries too many steps")
    for index, step in enumerate(plan.steps):
        _validate_voice_plan_step(step, index)
    if plan.kind == "plan":
        if not plan.steps or plan.expires_at_ms is None or plan.plan_digest is None:
            raise ValueError("a compiled plan requires steps, an expiry, and a digest")
        if plan.reason is not None or plan.options or plan.pending_intent_id is not None:
            raise ValueError("a compiled plan carries no reason, options, or pending intent")
    else:
        if plan.steps or plan.plan_digest is not None or plan.expires_at_ms is not None:
            raise ValueError("only a compiled plan carries steps, a digest, or an expiry")
        if plan.kind == "cancel_pending":
            if plan.pending_intent_id is None or plan.reason is not None or plan.options:
                raise ValueError("cancel_pending names exactly the pending intent")
        elif plan.reason is None or plan.pending_intent_id is not None:
            raise ValueError("clarify, unsupported, and refuse carry a typed reason")


def _validate_voice_plan_step(step: VoicePlanStep, index: int) -> None:
    if not isinstance(step, VoicePlanStep):
        raise ValueError("voice plan steps must be plan steps")
    if not isinstance(step.index, int) or isinstance(step.index, bool) or step.index != index:
        raise ValueError("voice plan steps must be indexed in order")
    if (
        not _bounded_text(step.intent_id, limit=128)
        or _CORRELATION_ID.fullmatch(step.intent_id) is None
    ):
        raise ValueError("voice plan step intent_id must be a safe bounded identifier")
    if not isinstance(step.name, str) or _INTENT_NAME.fullmatch(step.name) is None:
        raise ValueError("voice plan step name must be an intent name")
    if not isinstance(step.args, Mapping):
        raise ValueError("voice plan step args must be an object")
    _reject_non_json(step.args)
    if step.mode not in _MODES:
        raise ValueError("voice plan step mode is unsupported")
    if not isinstance(step.confirm_required, bool):
        raise ValueError("voice plan step confirmation requirement must be a boolean")
    if not isinstance(step.selection, tuple):
        raise ValueError("voice plan step selection must be a tuple")
    _positive_ids(list(step.selection))
    if not isinstance(step.notes, tuple) or len(step.notes) > MAX_VOICE_PLAN_NOTES:
        raise ValueError("voice plan step notes must be a bounded tuple")
    if any(not _bounded_text(note) for note in step.notes):
        raise ValueError("voice plan step notes must be bounded strings")
    candidate = {
        "v": 1,
        "t": 0,
        "type": "intent",
        "intent_id": step.intent_id,
        "retry_of": None,
        "source": "language",
        "session": "voice-plan-validation",
        "name": step.name,
        "args": _thaw(step.args),
        "selection": list(step.selection),
        "mode": step.mode,
        "confirm": True,
    }
    validated = validate_intent(
        candidate,
        capability_profile=CapabilityProfile("voice_schema", IMPLEMENTED_INTENT_NAMES),
    )
    if not isinstance(validated, AcceptedIntent):
        raise ValueError("voice plan step is not a canonical Intent v1 proposal")
    if step.confirm_required != (validated.intent.name in CONFIRMATION_REQUIRED_INTENTS):
        raise ValueError("voice plan step confirmation policy differs from the arbiter")


def _reject_non_json(value: object) -> None:
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int | float):
        if isinstance(value, float) and (value != value or value in (float("inf"), -float("inf"))):
            raise ValueError("voice plan step args must be finite numbers")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("voice plan step args keys must be strings")
            _reject_non_json(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _reject_non_json(item)
        return
    raise ValueError("voice plan step args must be JSON-native")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw(item) for item in value]
    return value


class TranscriptService:
    def __init__(
        self,
        *,
        transcription: TranscriptionTransport | None = None,
        compiler: TranscriptCompiler | None = None,
        tracer: VoiceTraceSink | None = None,
        duration_probe: Callable[[AudioUpload], int] | None = None,
    ) -> None:
        self._transcription = transcription or configured_transcription()
        self._provider = getattr(self._transcription, "provider", "whisper")
        self._model = getattr(self._transcription, "model", WHISPER_MODEL)
        self._compiler = compiler or UnavailableTranscriptCompiler()
        self._tracer = tracer or get_default_voice_trace_sink()
        self._duration_probe = duration_probe or probe_audio_duration_ms

    def process(
        self,
        *,
        session_id: str,
        correlation_id: str,
        content_type: str | None,
        body: bytes,
        relay_state: object,
        rooms: tuple[str, ...] = (),
        now_ms: int,
        refresh_state: Callable[[], tuple[object, int]] | None = None,
    ) -> VoiceOutcome:
        """Transcribe one upload and hand the transcript to the compiler.

        ``relay_state`` and ``now_ms`` are validated before any provider I/O. When
        ``refresh_state`` is given it is called after transcription and its
        ``(relay_state, now_ms)`` pair replaces them for compilation, so the plan is
        grounded on a state event younger than the compiler's maximum state age rather
        than one captured before the transcription round trip.
        """
        normalized_type = _normalized_content_type(content_type)
        failure = _upload_failure(correlation_id, normalized_type, body)
        if failure is not None:
            return failure
        assert normalized_type is not None
        upload = AudioUpload(normalized_type, body)
        try:
            measured_duration_ms = self._duration_probe(upload)
        except (OSError, ValueError):
            return VoiceOutcome("refused", "template", "invalid_audio", None)
        if measured_duration_ms > MAX_AUDIO_DURATION_MS:
            return VoiceOutcome("refused", "template", "audio_too_long", None)
        try:
            grounded_state = compiler_relay_state(relay_state)
            grounded_rooms = compiler_rooms(rooms)
            capability_version = compiler_capability_version(grounded_state)
        except ValueError:
            return VoiceOutcome("refused", "template", "invalid_relay_state", None)
        cost_usd = _whisper_cost(measured_duration_ms) if self._provider == "whisper" else None
        self._record(
            {
                "event": "voice_started",
                "correlation_id": correlation_id,
                "session_id": session_id,
                "model": self._model,
                "content_type": normalized_type,
                "bytes": len(body),
                "audio_duration_ms": measured_duration_ms,
                "provider_cost_usd": cost_usd,
                "combined_cost_usd": cost_usd,
            }
        )
        try:
            transcript = _validated_transcript(self._transcription.transcribe(upload))
        except TranscriptionError:
            return self._complete(
                VoiceOutcome("refused", "template", "transcription_unavailable", None),
                correlation_id=correlation_id,
                session_id=session_id,
                cost_usd=cost_usd,
            )
        except ValueError:
            return self._complete(
                VoiceOutcome("refused", "template", "invalid_transcript", None),
                correlation_id=correlation_id,
                session_id=session_id,
                cost_usd=cost_usd,
            )
        if refresh_state is not None:
            try:
                fresh_state, now_ms = refresh_state()
                grounded_state = compiler_relay_state(fresh_state)
                capability_version = compiler_capability_version(grounded_state)
                _nonnegative_integer(now_ms)
            except Exception:
                return self._complete(
                    VoiceOutcome("refused", "template", "invalid_relay_state", transcript),
                    correlation_id=correlation_id,
                    session_id=session_id,
                    cost_usd=cost_usd,
                )
        try:
            compiler_result = self._compiler.compile(
                transcript,
                grounded_state,
                capability_version=capability_version,
                rooms=grounded_rooms,
                now_ms=now_ms,
                correlation_id=correlation_id,
                session_id=session_id,
            )
        except CompilerUnavailable:
            return self._complete(
                VoiceOutcome("refused", "template", "compiler_unavailable", transcript),
                correlation_id=correlation_id,
                session_id=session_id,
                cost_usd=cost_usd,
            )
        except Exception:
            return self._complete(
                VoiceOutcome("refused", "template", "compiler_unavailable", transcript),
                correlation_id=correlation_id,
                session_id=session_id,
                cost_usd=cost_usd,
            )
        if not isinstance(compiler_result, tuple) or len(compiler_result) != 2:
            return self._complete(
                VoiceOutcome("refused", "template", "compiler_unavailable", transcript),
                correlation_id=correlation_id,
                session_id=session_id,
                cost_usd=cost_usd,
            )
        plan = compiler_result[0] if isinstance(compiler_result[0], VoicePlan) else None
        if plan is not None and (
            plan.transcript != transcript
            or plan.session != session_id
            or plan.correlation_id != correlation_id
            or plan.state_event_id != grounded_state["event_id"]
        ):
            return self._complete(
                VoiceOutcome("refused", "template", "compiler_unavailable", transcript),
                correlation_id=correlation_id,
                session_id=session_id,
                cost_usd=cost_usd,
            )
        return self._complete(
            VoiceOutcome("transcribed", self._provider, None, transcript, (), plan),
            correlation_id=correlation_id,
            session_id=session_id,
            cost_usd=cost_usd,
        )

    def _complete(
        self,
        outcome: VoiceOutcome,
        *,
        correlation_id: str,
        session_id: str,
        cost_usd: float | None,
    ) -> VoiceOutcome:
        self._record(
            {
                "event": "voice_completed",
                "correlation_id": correlation_id,
                "session_id": session_id,
                "status": outcome.status,
                "source": outcome.source,
                "reason": outcome.reason,
                "provider_cost_usd": cost_usd,
                "combined_cost_usd": cost_usd,
            }
        )
        return outcome

    def _record(self, event: Mapping[str, object]) -> None:
        try:
            self._tracer.record(event)
        except Exception:
            return


def compiler_relay_state(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping) or raw.get("type") != "state" or raw.get("mode") != "indoor":
        raise ValueError("relay state must be an indoor state event")
    timestamp = _nonnegative_integer(raw.get("t"))
    roster_version = _nonnegative_integer(raw.get("roster_version"))
    event_id = _bounded_state_identifier(raw.get("event_id"))
    session = _bounded_state_identifier(raw.get("session"))
    armed = raw.get("armed")
    estop = raw.get("estop")
    if not isinstance(armed, bool) or not isinstance(estop, bool):
        raise ValueError("relay state requires safety flags")
    selection = _positive_ids(raw.get("selection"))
    drones_raw = raw.get("drones")
    if not isinstance(drones_raw, list) or len(drones_raw) > 4:
        raise ValueError("relay state requires a bounded drone list")
    drones: list[dict[str, object]] = []
    known_ids: set[int] = set()
    for item in drones_raw:
        if not isinstance(item, Mapping):
            raise ValueError("relay drone must be an object")
        drone_id = _positive_integer(item.get("drone_id"))
        if drone_id in known_ids:
            raise ValueError("relay drone IDs must be unique")
        known_ids.add(drone_id)
        membership = item.get("membership")
        selectable = item.get("selectable")
        flight_state = item.get("flight_state")
        camera_patterns = item.get("camera_patterns")
        capabilities = item.get("adapter_capabilities")
        if membership not in _MEMBERSHIPS or not isinstance(selectable, bool):
            raise ValueError("relay drone readiness is invalid")
        if flight_state is not None and flight_state not in _FLIGHT_STATES:
            raise ValueError("relay drone flight state is invalid")
        if not isinstance(camera_patterns, list) or any(
            pattern not in _CAMERA_PATTERNS for pattern in camera_patterns
        ):
            raise ValueError("relay camera capabilities are invalid")
        if not isinstance(capabilities, list) or any(
            not isinstance(capability, str) for capability in capabilities
        ):
            raise ValueError("relay adapter capabilities are invalid")
        drones.append(
            {
                "drone_id": drone_id,
                "membership": membership,
                "selectable": selectable,
                "flight_state": flight_state,
                "camera_patterns": sorted(camera_patterns),
                "adapter_capabilities": ["flight"] if "flight" in capabilities else [],
            }
        )
    if any(drone_id not in known_ids for drone_id in selection):
        raise ValueError("selection references an unknown drone")
    grounded: dict[str, object] = {
        "v": 1,
        "t": timestamp,
        "type": "state",
        "event_id": event_id,
        "session": session,
        "roster_version": roster_version,
        "armed": armed,
        "estop": estop,
        "selection": selection,
        "mode": "indoor",
        "drones": drones,
    }
    # The advertised capability profile and the pending intent are the only other
    # facts the compiler grounds on; both are copied only in their documented shapes.
    if "capability_profile" in raw or "enabled_intent_names" in raw:
        profile = raw.get("capability_profile")
        enabled = raw.get("enabled_intent_names")
        if (
            not isinstance(profile, str)
            or not profile
            or len(profile) > 64
            or not isinstance(enabled, list)
            or not enabled
            or any(
                not isinstance(name, str) or _INTENT_NAME.fullmatch(name) is None
                for name in enabled
            )
        ):
            raise ValueError("relay capability profile advertisement is invalid")
        grounded["capability_profile"] = profile
        grounded["enabled_intent_names"] = sorted(set(enabled))
    pending = raw.get("pending")
    if (
        isinstance(pending, Mapping)
        and isinstance(pending.get("intent_id"), str)
        and isinstance(pending.get("name"), str)
    ):
        grounded["pending"] = {"intent_id": pending["intent_id"], "name": pending["name"]}
    return grounded


def compiler_rooms(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, tuple) or len(raw) > 64:
        raise ValueError("rooms must be a bounded tuple")
    if any(not isinstance(room, str) or _ROOM_ID.fullmatch(room) is None for room in raw):
        raise ValueError("rooms must be unique safe identifiers")
    if len(set(raw)) != len(raw):
        raise ValueError("rooms must be unique safe identifiers")
    return raw


def compiler_capability_version(relay_state: Mapping[str, object]) -> str:
    projection = {
        "v": 1,
        "mode": relay_state["mode"],
        "drones": [
            {
                "drone_id": drone["drone_id"],
                "membership": drone["membership"],
                "selectable": drone["selectable"],
                "camera_patterns": drone["camera_patterns"],
                "adapter_capabilities": drone["adapter_capabilities"],
            }
            for drone in relay_state["drones"]
        ],
    }
    encoded = json.dumps(projection, separators=(",", ":"), sort_keys=True).encode()
    return "relay-capabilities-" + hashlib.sha256(encoded).hexdigest()[:16]


def transcription_request_key(upload: AudioUpload, *, model: str = WHISPER_MODEL) -> str:
    request = {
        "model": model,
        "schema": "voice-transcription-v1",
        "content_type": upload.content_type,
        "audio_sha256": hashlib.sha256(upload.body).hexdigest(),
    }
    if model == DEEPGRAM_MODEL:
        request["options"] = {
            "language": "en",
            "smart_format": True,
            "keyterms": list(COMMAND_KEYTERMS),
        }
    canonical = json.dumps(request, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_valid_correlation_id(value: str | None) -> bool:
    return value is not None and _CORRELATION_ID.fullmatch(value) is not None


def _upload_failure(
    correlation_id: str,
    content_type: str | None,
    body: bytes,
) -> VoiceOutcome | None:
    if not is_valid_correlation_id(correlation_id):
        return VoiceOutcome("refused", "template", "invalid_correlation_id", None)
    if content_type not in ALLOWED_AUDIO_CONTENT_TYPES:
        return VoiceOutcome("refused", "template", "unsupported_content_type", None)
    if not body:
        return VoiceOutcome("refused", "template", "empty_upload", None)
    if len(body) > MAX_AUDIO_BYTES:
        return VoiceOutcome("refused", "template", "upload_too_large", None)
    return None


def _normalized_content_type(value: str | None) -> str | None:
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower()


def _validated_transcript(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("transcript must be a string")
    transcript = value.strip()
    if not transcript or len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise ValueError("transcript is empty or too long")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in transcript):
        raise ValueError("transcript contains control characters")
    return transcript


def _whisper_cost(audio_duration_ms: int | None) -> float | None:
    if audio_duration_ms is None:
        return None
    if not isinstance(audio_duration_ms, int) or isinstance(audio_duration_ms, bool):
        return None
    if audio_duration_ms < 0 or audio_duration_ms > MAX_AUDIO_DURATION_MS:
        return None
    return round(audio_duration_ms / 60_000 * WHISPER_USD_PER_MINUTE, 8)


def _ceil_ms(duration_seconds: Fraction) -> int:
    duration_ms = duration_seconds * 1_000
    return (duration_ms.numerator + duration_ms.denominator - 1) // duration_ms.denominator


def probe_audio_duration_ms(upload: AudioUpload) -> int:
    try:
        with av.open(io.BytesIO(upload.body)) as container:
            audio_streams = [stream for stream in container.streams if stream.type == "audio"]
            if len(audio_streams) != 1:
                raise ValueError("upload must contain one audio stream")
            decoded_duration = Fraction()
            previous_frame_start: Fraction | None = None
            for frame in container.decode(audio=0):
                if not frame.sample_rate or frame.sample_rate < 0 or frame.samples <= 0:
                    raise ValueError("audio sample rate is unavailable")
                if frame.pts is None or frame.time_base is None or frame.time_base <= 0:
                    raise ValueError("audio frame timestamp is unavailable")
                frame_start = Fraction(frame.pts) * Fraction(frame.time_base)
                if frame_start < 0 or (
                    previous_frame_start is not None and frame_start <= previous_frame_start
                ):
                    raise ValueError("audio frame timestamps are invalid")
                previous_frame_start = frame_start
                decoded_duration += Fraction(frame.samples, frame.sample_rate)
                if decoded_duration * 1_000 > MAX_AUDIO_DURATION_MS:
                    return _ceil_ms(decoded_duration)
    except av.error.FFmpegError as error:
        raise ValueError("audio cannot be decoded") from error
    if decoded_duration <= 0:
        raise ValueError("audio duration is unavailable")
    return _ceil_ms(decoded_duration)


def _extension(content_type: str) -> str:
    return {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/mpeg": ".mp3",
    }[content_type]


def _nonnegative_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("value must be a non-negative integer")
    return value


def _positive_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("value must be a positive integer")
    return value


def _positive_ids(value: object) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("selection must be a list")
    ids = [_positive_integer(item) for item in value]
    if len(ids) != len(set(ids)):
        raise ValueError("selection must be unique")
    return ids


def _bounded_state_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("relay state identifier is invalid")
    return value
