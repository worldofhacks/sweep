"""Measure provider transcription on recorded audio and replay the captured responses."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
from collections.abc import Sequence
from pathlib import Path

from relay.voice import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_DURATION_MS,
    AudioUpload,
    RecordingTranscriptionTransport,
    ReplayTranscriptionTransport,
    TranscriptionError,
    configured_transcription,
    probe_audio_duration_ms,
)


def word_errors(expected: str, actual: str) -> tuple[int, int]:
    reference = re.findall(r"\w+", expected.casefold())
    hypothesis = re.findall(r"\w+", actual.casefold())
    previous = list(range(len(hypothesis) + 1))
    for index, word in enumerate(reference, 1):
        current = [index]
        for column, other in enumerate(hypothesis, 1):
            current.append(
                min(
                    previous[column] + 1,
                    current[-1] + 1,
                    previous[column - 1] + (word != other),
                )
            )
        previous = current
    return previous[-1], len(reference)


def run(manifest: Path, output: Path, *, replay: bool = False) -> dict[str, object]:
    cases = json.loads(manifest.read_text())
    if not isinstance(cases, list) or len(cases) != 20:
        raise ValueError("speech smoke manifest must contain exactly 20 recorded utterances")
    prepared = []
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "audio", "content_type", "transcript"}:
            raise ValueError("each speech case needs id, audio, content_type, transcript")
        if any(not isinstance(value, str) or not value.strip() for value in case.values()):
            raise ValueError("speech case fields must be nonempty strings")
        if case["id"] in ids:
            raise ValueError("speech case IDs must be unique")
        ids.add(case["id"])
        audio_path = manifest.parent / case["audio"]
        if audio_path.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("speech audio exceeds upload byte limit")
        upload = AudioUpload(case["content_type"], audio_path.read_bytes())
        duration = probe_audio_duration_ms(upload)
        if duration > MAX_AUDIO_DURATION_MS:
            raise ValueError("speech audio exceeds upload duration limit")
        prepared.append((case, upload, duration))
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for provider in ("deepgram", "whisper"):
        cassette = output / f"voice-{provider}-recorded.json"
        transport = (
            ReplayTranscriptionTransport(cassette, provider=provider)
            if replay
            else RecordingTranscriptionTransport(
                configured_transcription(provider=provider), cassette
            )
        )
        for case, upload, duration in prepared:
            started = time.perf_counter()
            error = None
            try:
                transcript = transport.transcribe(upload)
            except (TranscriptionError, ValueError):
                transcript = ""
                error = "transcription_failed"
            elapsed_ms = (time.perf_counter() - started) * 1000
            edits, words = word_errors(case["transcript"], transcript)
            rows.append(
                {
                    "id": case["id"],
                    "provider": provider,
                    "model": transport.model,
                    "source": "replay" if replay else "live",
                    "transcript": transcript,
                    "error": error,
                    "word_errors": edits,
                    "reference_words": words,
                    "latency_ms": None if replay else round(elapsed_ms, 3),
                    "audio_duration_ms": duration,
                    "audio_sha256": hashlib.sha256(upload.body).hexdigest(),
                }
            )
    summary = {}
    for provider in ("deepgram", "whisper"):
        selected = [row for row in rows if row["provider"] == provider]
        errors = sum(row["word_errors"] for row in selected)
        words = sum(row["reference_words"] for row in selected)
        latencies = sorted(row["latency_ms"] for row in selected if row["latency_ms"] is not None)
        summary[provider] = {
            "cases": len(selected),
            "failures": sum(row["error"] is not None for row in selected),
            "word_accuracy": 1 - errors / words if words else None,
            "median_ms": statistics.median(latencies) if latencies else None,
            "p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
        }
    result = {
        "version": 1,
        "source": "replay" if replay else "live",
        "summary": summary,
        "rows": rows,
    }
    name = "replay-results.json" if replay else "live-results.json"
    (output / name).write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(args.manifest, args.output, replay=args.replay)
    except (OSError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    print(json.dumps(result["summary"], indent=2))
    return int(any(row["error"] for row in result["rows"]))


if __name__ == "__main__":
    raise SystemExit(main())
