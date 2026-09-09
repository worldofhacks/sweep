"""Bounded local inspection. Embedded timestamps/GPS are claims, never trusted camera fixes."""

import json
import math
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image

from relay.atlas import AtlasError


def command(arguments, maximum=128_000, timeout=15):
    if not shutil.which(arguments[0]):
        raise AtlasError(
            f"{arguments[0]} is unavailable on the server. Originals are unchanged.", 503
        )
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AtlasError("Media inspection could not finish within its limit.", 422) from None
        if result.returncode:
            raise AtlasError("This original could not be decoded for context.", 422)
        output.seek(0)
        data = output.read(maximum + 1)
        if len(data) > maximum:
            raise AtlasError("The media inspection output exceeded its limit.", 422)
        return data


def input_format(path: Path):
    """Admit binary containers before invoking a decoder; never playlists or URL input."""
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith((b"RIFF", b"RF64")) and header[8:12] == b"WAVE":
        return "wav"
    if header[4:8] == b"ftyp":
        return "mov"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"fLaC"):
        return "flac"
    if header.startswith(b"ID3") or (
        len(header) > 1 and header[0] == 255 and header[1] & 0xE0 == 0xE0
    ):
        # JPEG has a different marker; keep it on the image decoder path below.
        if not header.startswith(b"\xff\xd8"):
            return "mp3"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg_pipe"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png_pipe"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp_pipe"
    raise AtlasError("This binary media container is not supported for context inspection.", 415)


def probe(path: Path):
    return json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-max_alloc",
                "67108864",
                "-probesize",
                "5000000",
                "-analyzeduration",
                "5000000",
                "-show_entries",
                "format=duration,format_name:format_tags=creation_time,location,com.apple.quicktime.location.ISO6709:"
                "stream=codec_type,codec_name,width,height,channels,sample_rate:stream_tags=creation_time",
                "-f",
                input_format(path),
                "-of",
                "json",
                str(path),
            ]
        )
    )


def admitted_asset(path, claimed):
    supported = {
        "audio/mpeg",
        "audio/mp4",
        "audio/x-m4a",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "audio/ogg",
        "audio/flac",
        "video/mp4",
        "video/webm",
    }
    if claimed not in supported:
        raise AtlasError("Use MP3, M4A, WAV, Ogg, FLAC, WebM, or MP4 with an audio track.", 415)
    value = probe(path)
    if not any(stream.get("codec_type") == "audio" for stream in value.get("streams", [])):
        raise AtlasError("This memory track has no decodable audio stream.", 415)
    formats = set(value.get("format", {}).get("format_name", "").split(","))
    expected = {
        "audio/mpeg": {"mp3"},
        "audio/mp4": {"mov", "mp4"},
        "audio/x-m4a": {"mov", "mp4"},
        "audio/wav": {"wav"},
        "audio/x-wav": {"wav"},
        "audio/webm": {"matroska", "webm"},
        "audio/ogg": {"ogg"},
        "audio/flac": {"flac"},
        "video/mp4": {"mov", "mp4"},
        "video/webm": {"matroska", "webm"},
    }
    if not formats.intersection(expected[claimed]):
        raise AtlasError("The audio container does not match the declared type.", 415)
    return {"audio/x-wav": "audio/wav", "audio/x-m4a": "audio/mp4"}.get(claimed, claimed)


def aware_time(value):
    try:
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return instant.isoformat() if instant.tzinfo else None
    except (ValueError, TypeError):
        return None


def gps_coordinate(parts, reference):
    result = sum(float(n) / divisor for n, divisor in zip(parts, (1, 60, 3600), strict=True))
    return -result if reference in ("S", "W", b"S", b"W") else result


def inspect_media(path, mime):
    result = {
        "mime": mime,
        "has_audio": False,
        "warnings": [],
        "timestamp": None,
        "local_timestamp": None,
        "location": None,
    }
    if mime.startswith("image/"):
        try:
            with Image.open(path) as image:
                result.update(width=image.width, height=image.height, format=image.format)
                exif = image.getexif()
                details = exif.get_ifd(ExifTags.IFD.Exif)
                raw = details.get(36867)
                if raw:
                    local = datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").isoformat()
                    result["local_timestamp"] = local
                    result["timestamp"] = aware_time(local + str(details.get(36881, "")))
                gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
                if all(key in gps for key in (1, 2, 3, 4)):
                    lat, lon = gps_coordinate(gps[2], gps[1]), gps_coordinate(gps[4], gps[3])
                    if (
                        math.isfinite(lat)
                        and math.isfinite(lon)
                        and -85 <= lat <= 85
                        and -180 <= lon <= 180
                    ):
                        result["location"] = {"latitude": lat, "longitude": lon}
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ZeroDivisionError,
            Image.DecompressionBombError,
        ):
            result["warnings"].append("Some image metadata could not be read.")
    else:
        info = probe(path)
        streams, format_info = info.get("streams", []), info.get("format", {})
        tags = format_info.get("tags", {})
        result["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
        duration = float(format_info.get("duration", 0))
        if math.isfinite(duration) and duration >= 0:
            result["duration_seconds"] = duration
        for stream in streams:
            if stream.get("codec_type") == "video":
                result.update(width=stream.get("width"), height=stream.get("height"))
                if not tags.get("creation_time"):
                    tags = {**tags, **stream.get("tags", {})}
                break
        result["timestamp"] = aware_time(tags.get("creation_time"))
        raw = tags.get("com.apple.quicktime.location.ISO6709", tags.get("location", ""))
        match = re.fullmatch(r"([+-]\d{2,3}\.\d+)([+-]\d{2,3}\.\d+)(?:[+-][\d.]+)?/?", raw)
        if match:
            lat, lon = map(float, match.groups())
            if -85 <= lat <= 85 and -180 <= lon <= 180:
                result["location"] = {"latitude": lat, "longitude": lon}
    if result["local_timestamp"] and not result["timestamp"]:
        result["warnings"].append(
            "Embedded date has no UTC offset. Confirm its timezone before weather lookup."
        )
    result["warnings"].append(
        "Embedded metadata may reflect editing/export. Confirm it; "
        "original provenance is unchanged."
    )
    return result


def audio_sample(path):
    """A bounded derivative; never replace or modify the original file."""
    return command(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-threads",
            "1",
            "-max_alloc",
            "67108864",
            "-f",
            input_format(path),
            "-i",
            str(path),
            "-t",
            "60",
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "pipe:1",
        ],
        maximum=2_000_000,
        timeout=20,
    )


def preview_frame(path):
    info = probe(path)
    if any(s.get("width", 0) * s.get("height", 0) > 40_000_000 for s in info.get("streams", [])):
        raise AtlasError("The original is too large for an AI preview. It is unchanged.", 422)
    return command(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-threads",
            "1",
            "-max_alloc",
            "67108864",
            "-f",
            input_format(path),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=1024:1024:force_original_aspect_ratio=decrease",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ],
        maximum=2_000_000,
    )
