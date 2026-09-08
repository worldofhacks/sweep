"""Combine verified single-camera capture snapshots into calibration evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

_SESSION_VERSION = "ohmni-calibration-session/v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _safe_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise ValueError(f"{label} is unsafe")
    return value


def _source(
    directory: Path,
) -> tuple[dict[str, object], dict[str, object], str, dict[str, object]]:
    result_path = directory / "result.json"
    result = _json(result_path, "capture result")
    provenance = result.get("recorded_live_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("capture result lacks recorded-live provenance")
    manifest_name = _safe_name(provenance.get("manifest_file"), "capture manifest file")
    manifest_hash = provenance.get("manifest_sha256")
    stream = provenance.get("stream_id")
    if not isinstance(manifest_hash, str) or not isinstance(stream, str) or not stream:
        raise ValueError("capture provenance is invalid")
    manifest_path = directory / manifest_name
    if not manifest_path.is_file() or _sha256(manifest_path) != manifest_hash:
        raise ValueError("capture manifest is missing or changed")
    manifest = _json(manifest_path, "capture manifest")
    cameras = manifest.get("cameras")
    pipeline = manifest.get("capture_pipeline")
    pipeline_hash = manifest.get("capture_pipeline_sha256")
    if (
        manifest.get("schema_version") != "ohmni-dual-calibration-capture/v2"
        or manifest.get("status") != "complete"
        or not isinstance(cameras, dict)
        or not isinstance(pipeline, dict)
        or pipeline != cameras
        or not isinstance(pipeline_hash, str)
        or (
            hashlib.sha256(
                json.dumps(pipeline, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            != pipeline_hash
        )
        or stream not in cameras
    ):
        raise ValueError("capture manifest lacks a valid pipeline binding")
    stream_pipeline = cameras[stream]
    if not isinstance(stream_pipeline, dict):
        raise ValueError("capture stream pipeline is invalid")
    calibration_pipeline = stream_pipeline.get("calibration_pipeline")
    if not isinstance(calibration_pipeline, dict):
        raise ValueError("capture calibration pipeline is invalid")
    frames = result.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("capture result has no frames")
    return result, manifest, stream, stream_pipeline


def create_session(output: Path, sources: list[Path]) -> dict[str, object]:
    if len(sources) < 2:
        raise ValueError("a calibration session requires at least two captures")
    if output.exists():
        raise FileExistsError(output)
    descriptors = [_source(path) for path in sources]
    stream = descriptors[0][2]
    camera_pipeline = descriptors[0][3]
    if any(item[2] != stream or item[3] != camera_pipeline for item in descriptors[1:]):
        raise ValueError("session captures must use the same camera pipeline")
    collections = [item[1].get("raw_capture_collection") for item in descriptors]
    if any(not isinstance(collection, str) or not collection for collection in collections) or len(
        set(collections)
    ) != len(collections):
        raise ValueError("session captures must have distinct raw collections")
    temporary = output.parent / f".{output.name}.pending-{uuid4().hex}"
    temporary.mkdir(parents=True)
    try:
        session_sources: list[dict[str, object]] = []
        aggregate_frames: list[dict[str, object]] = []
        result_frames: list[dict[str, object]] = []
        aggregate_index = 0
        for source_index, (source, descriptor) in enumerate(zip(sources, descriptors, strict=True)):
            result, manifest, source_stream, source_pipeline = descriptor
            source_dir = temporary / "sources" / f"source-{source_index:06}"
            source_dir.mkdir(parents=True)
            copied: dict[str, str] = {}
            source_result = source / "result.json"
            source_provenance = result.get("recorded_live_provenance")
            assert isinstance(source_provenance, dict)
            source_manifest = source / _safe_name(
                source_provenance.get("manifest_file"), "capture manifest file"
            )
            for source_file in (source_result, source_manifest):
                destination = source_dir / source_file.name
                shutil.copyfile(source_file, destination)
                copied[str(destination.relative_to(temporary))] = _sha256(destination)
            source_frames = result["frames"]
            assert isinstance(source_frames, list)
            seen_indices: set[int] = set()
            frame_bindings: list[dict[str, object]] = []
            for frame in source_frames:
                if not isinstance(frame, dict) or type(frame.get("frame_index")) is not int:
                    raise ValueError("capture result frame is invalid")
                original_index = frame["frame_index"]
                if original_index in seen_indices:
                    raise ValueError("capture result has duplicate frame indexes")
                seen_indices.add(original_index)
                image_name = _safe_name(frame.get("image_file"), "capture image file")
                raw_name = _safe_name(frame.get("source_file"), "capture raw file")
                record_name = f"frame-{original_index:06}.json"
                source_record = source / record_name
                if not source_record.is_file() or _json(source_record, "capture frame") != frame:
                    raise ValueError("capture result is not bound to its frame record")
                for source_file in (source_record, source / image_name, source / raw_name):
                    if not source_file.is_file():
                        raise ValueError("capture source file is missing")
                    destination = source_dir / source_file.name
                    shutil.copyfile(source_file, destination)
                    copied[str(destination.relative_to(temporary))] = _sha256(destination)
                destination_image = temporary / f"frame-{aggregate_index:06}.png"
                destination_raw = temporary / f"raw-{aggregate_index:06}{Path(raw_name).suffix}"
                shutil.copyfile(source / image_name, destination_image)
                shutil.copyfile(source / raw_name, destination_raw)
                aggregate = dict(frame)
                aggregate.update(
                    frame_index=aggregate_index,
                    image_file=destination_image.name,
                    image_sha256=_sha256(destination_image),
                    source_file=destination_raw.name,
                    source_sha256=_sha256(destination_raw),
                    source_session_index=source_index,
                    source_frame_index=original_index,
                )
                result_frames.append(aggregate)
                aggregate_frames.append(
                    {
                        "frame_index": aggregate_index,
                        "source_session_index": source_index,
                        "source_frame_index": original_index,
                        "image_file": destination_image.name,
                        "image_sha256": aggregate["image_sha256"],
                        "source_file": destination_raw.name,
                        "source_sha256": aggregate["source_sha256"],
                    }
                )
                frame_bindings.append(
                    {
                        "source_frame_index": original_index,
                        "frame_file": str((source_dir / record_name).relative_to(temporary)),
                        "image_file": str((source_dir / image_name).relative_to(temporary)),
                        "source_file": str((source_dir / raw_name).relative_to(temporary)),
                        "image_sha256": frame.get("image_sha256"),
                        "source_sha256": frame.get("source_sha256"),
                    }
                )
                aggregate_index += 1
            session_sources.append(
                {
                    "source_session_index": source_index,
                    "stream_id": source_stream,
                    "boot_id": manifest.get("boot_id"),
                    "raw_capture_collection": manifest.get("raw_capture_collection"),
                    "capture_pipeline_sha256": manifest.get("capture_pipeline_sha256"),
                    "camera_pipeline": source_pipeline,
                    "result_file": str((source_dir / "result.json").relative_to(temporary)),
                    "result_sha256": _sha256(source_result),
                    "manifest_file": str(
                        (source_dir / source_manifest.name).relative_to(temporary)
                    ),
                    "manifest_sha256": _sha256(source_manifest),
                    "files": copied,
                    "frames": frame_bindings,
                }
            )
        session = {
            "schema_version": _SESSION_VERSION,
            "status": "complete",
            "stream_id": stream,
            "camera_pipeline": camera_pipeline,
            "sources": session_sources,
            "frames": aggregate_frames,
        }
        session_path = temporary / "session.json"
        session_path.write_text(json.dumps(session, sort_keys=True) + "\n")
        result = {
            "frames": result_frames,
            "recorded_live_provenance": {
                "session_file": session_path.name,
                "session_sha256": _sha256(session_path),
                "stream_id": stream,
            },
        }
        (temporary / "result.json").write_text(json.dumps(result, sort_keys=True) + "\n")
        os.replace(temporary, output)
        return result
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    args = parser.parse_args()
    result = create_session(args.output, args.source)
    print(json.dumps({"output": str(args.output), "frames": len(result["frames"])}))


if __name__ == "__main__":
    main()
