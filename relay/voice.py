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

from relay.language_runtime import LanguageCompilationOutcome
from relay.voice_telemetry import VoiceTraceSink, get_default_voice_trace_sink

WHISPER_MODEL = "whisper-1"
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
    def __init__(self, *, api_key: str | None = None, timeout_s: float = 20.0) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    def transcribe(self, upload: AudioUpload) -> str:
        api_key = self._api_key or os.environ.get("OPENAI_API_KEY")
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


class ReplayTranscriptionTransport:
    def __init__(self, cassette_path: Path) -> None:
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
        entry = self._entries.get(transcription_request_key(upload))
        if not isinstance(entry, Mapping) or set(entry) != {"text"}:
            raise TranscriptionError(f"replay miss for {transcription_request_key(upload)}")
        return _validated_transcript(entry.get("text"))


class RecordingTranscriptionTransport:
    def __init__(self, transport: TranscriptionTransport, cassette_path: Path) -> None:
        self._transport = transport
        self._cassette_path = cassette_path

    def transcribe(self, upload: AudioUpload) -> str:
        transcript = self._transport.transcribe(upload)
        cassette = self._load()
        entries = cassette["entries"]
        assert isinstance(entries, dict)
        entries[transcription_request_key(upload)] = {"text": transcript}
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
class VoiceOutcome:
    status: Literal["transcribed", "refused"]
    source: Literal["whisper", "template"]
    reason: str | None
    transcript: str | None
    compilation: LanguageCompilationOutcome | None = None
    emissions: tuple[()] = ()

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
            "compilation": None if self.compilation is None else self.compilation.to_dict(),
            "emissions": [],
        }


class TranscriptService:
    def __init__(
        self,
        *,
        transcription: TranscriptionTransport | None = None,
        compiler: TranscriptCompiler | None = None,
        tracer: VoiceTraceSink | None = None,
        duration_probe: Callable[[AudioUpload], int] | None = None,
    ) -> None:
        self._transcription = transcription or OpenAIWhisperTransport()
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
    ) -> VoiceOutcome:
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
        cost_usd = _whisper_cost(measured_duration_ms)
        self._record(
            {
                "event": "voice_started",
                "correlation_id": correlation_id,
                "session_id": session_id,
                "model": WHISPER_MODEL,
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
        compilation = compiler_result[0]
        return self._complete(
            VoiceOutcome(
                "transcribed",
                "whisper",
                None,
                transcript,
                compilation if isinstance(compilation, LanguageCompilationOutcome) else None,
            ),
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
    return {
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


def transcription_request_key(upload: AudioUpload) -> str:
    canonical = json.dumps(
        {
            "model": WHISPER_MODEL,
            "schema": "voice-transcription-v1",
            "content_type": upload.content_type,
            "audio_sha256": hashlib.sha256(upload.body).hexdigest(),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
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
