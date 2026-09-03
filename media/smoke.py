"""Run the one-source MediaMTX acceptance path and emit JSONL evidence."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from media.runtime import LatencyTrace, stream_path
from relay.app import RelayRuntime
from relay.contracts import MembershipAction, MembershipRequest
from relay.session import RelaySession
from relay.settings import RelaySettings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", type=int, default=1)
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--evidence", type=Path, default=Path(".sweep/media-smoke.jsonl"))
    parser.add_argument("--assert-retention", action="store_true")
    parser.add_argument("--retention-timeout", type=int, default=12)
    args = parser.parse_args()

    credentials = _credentials()
    stream = stream_path(args.stream)
    if args.duration < 2:
        raise RuntimeError("--duration must be at least 2 seconds")
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    recorder = Evidence(args.evidence)
    _wait_for_api(credentials["admin"])
    existing_recordings = set((Path("recordings") / stream).glob("*.mp4"))
    publisher_credential = credentials[f"publisher_{args.stream}"]
    cross_path = "drone2" if args.stream == 1 else "drone1"
    _expect_publish_refused(cross_path, publisher_credential)
    recorder.write("cross_path_publish_refused", stream=cross_path, t=_epoch_ms())
    _expect_publish_refused("drone6", credentials["reader"])
    recorder.write("reader_publish_refused", stream="drone6", t=_epoch_ms())
    _expect_http_status(
        "http://127.0.0.1:9997/v3/paths/list",
        None,
        expected=(401,),
    )
    recorder.write("anonymous_api_refused", t=_epoch_ms())
    source_started = _epoch_ms()
    trace = LatencyTrace(source_started_at_ms=source_started)
    with tempfile.TemporaryDirectory(prefix="sweep-media-relay-") as log_dir:
        relay = _relay_runtime(Path(log_dir), credentials["admin"])
        session = relay.session("media-smoke")
        session.registry.apply_join(_join_request(args.stream, source_started))
        publisher = _publisher(stream, publisher_credential, args.duration)
        recorder.write("source_started", stream=stream, t=source_started)

        try:
            api_path = _wait_for_path(stream, credentials["admin"])
            observed = _epoch_ms()
            trace.mark_path_ready(observed)
            recorder.write("path_ready", stream=stream, t=observed)
            _expect_publish_refused(stream, publisher_credential)
            recorder.write("publisher_override_refused", stream=stream, t=_epoch_ms())

            decoded = _wait_for_hls_decode(stream, credentials["reader"])
            observed = _epoch_ms()
            trace.mark_hls_frame_decoded(observed)
            recorder.write("hls_frame_decoded", stream=stream, t=observed, **decoded)

            browser = _run_browser_acceptance(credentials["reader"])
            recorder.write(stream=stream, t=_epoch_ms(), **browser)

            _expect_http_status(
                f"http://127.0.0.1:8888/{stream}/index.m3u8",
                None,
                expected=(401,),
            )
            recorder.write("anonymous_read_refused", stream=stream, t=_epoch_ms())
            _record_relay_projection(recorder, relay, session, args.stream, "live")
        except Exception as error:
            recorder.write("failed", stream=stream, t=_epoch_ms(), detail=str(error))
            publisher.terminate()
            publisher.wait(timeout=5)
            raise

        publisher.wait(timeout=args.duration + 5)
        if publisher.returncode != 0:
            raise RuntimeError(publisher.stderr.read().strip())
        _wait_for_path_unready(stream, credentials["admin"])
        _record_relay_projection(recorder, relay, session, args.stream, "offline")
        recovered_source = _publisher(stream, publisher_credential, 2)
        _wait_for_path_progress(
            stream,
            credentials["admin"],
            prior_bytes=api_path.get("bytesReceived"),
        )
        _record_relay_projection(recorder, relay, session, args.stream, "live")
        recovered_source.wait(timeout=7)
        if recovered_source.returncode != 0:
            raise RuntimeError(recovered_source.stderr.read().strip())
    recording = _wait_for_recording(stream, existing_recordings)
    recording_probe = _probe_recording(recording)
    recorder.write(
        "recording_decoded",
        stream=stream,
        t=_epoch_ms(),
        path=str(recording),
        bytes=recording.stat().st_size,
        **recording_probe,
    )
    recorder.write("latency_report", **trace.report(stream=stream))
    if args.assert_retention:
        _wait_for_retention_deletion(stream, existing_recordings, args.retention_timeout)
        recorder.write("retention_deleted", stream=stream, t=_epoch_ms())
    print(json.dumps(trace.report(stream=stream), sort_keys=True))
    return 0


class Evidence:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.write_text("", encoding="utf-8")

    def write(self, event: str, **fields: object) -> None:
        with self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _relay_runtime(log_dir: Path, admin: tuple[str, str]) -> RelayRuntime:
    return RelayRuntime(
        RelaySettings(
            relay_token=b"media-smoke-relay-token-at-least-32-bytes",
            log_dir=log_dir,
            media_api_url="http://127.0.0.1:9997",
            media_admin_username=admin[0],
            media_admin_password=admin[1],
            media_stale_after_ms=1_000,
        )
    )


def _join_request(drone_id: int, observed_at_ms: int) -> MembershipRequest:
    return MembershipRequest(
        v=1,
        t=observed_at_ms,
        type="membership",
        event_id="media-smoke-join",
        session="media-smoke",
        drone_id=drone_id,
        action=MembershipAction.JOIN,
        signature="media-smoke",
        adapter_id=f"media-smoke-adapter-{drone_id}",
        capabilities=("camera",),
    )


def _record_relay_projection(
    recorder: Evidence,
    runtime: RelayRuntime,
    session: RelaySession,
    drone_id: int,
    expected_status: str,
) -> None:
    asyncio.run(runtime.refresh_media_projection())
    state = session.current_state()
    drone = next(item for item in state["drones"] if item["drone_id"] == drone_id)
    video = drone.get("video")
    if not isinstance(video, dict) or video.get("status") != expected_status:
        raise RuntimeError(f"relay state expected video {expected_status!r}, received {video!r}")
    if set(video) != {"status", "last_frame_at"}:
        raise RuntimeError(f"relay video projection exposed unexpected fields: {video!r}")
    recorder.write(
        "relay_state_projection",
        envelope_type=state["type"],
        event_id=state["event_id"],
        session=state["session"],
        drone_id=drone_id,
        **video,
    )


def _credentials() -> dict[str, tuple[str, str]]:
    names = {
        **{
            f"publisher_{drone_id}": (
                f"sweep-publisher-{drone_id}",
                f"SWEEP_MEDIA_PUBLISH_PASSWORD_DRONE_{drone_id}",
            )
            for drone_id in range(1, 7)
        },
        "reader": ("sweep-reader", "SWEEP_MEDIA_READ_PASSWORD"),
        "admin": ("sweep-admin", "SWEEP_MEDIA_ADMIN_PASSWORD"),
    }
    result: dict[str, tuple[str, str]] = {}
    for role, (username, variable) in names.items():
        password = os.environ.get(variable, "")
        if not password:
            raise RuntimeError(f"{variable} is required")
        result[role] = (username, password)
    return result


def _publisher(stream: str, credential: tuple[str, str], duration: int) -> subprocess.Popen[str]:
    username, password = credential
    return subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-bf",
            "0",
            "-g",
            "30",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            f"rtsp://{username}:{password}@127.0.0.1:8554/{stream}",
        ],
        stderr=subprocess.PIPE,
        text=True,
    )


def _expect_publish_refused(stream: str, credential: tuple[str, str]) -> None:
    username, password = credential
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=16x16:rate=1",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            f"rtsp://{username}:{password}@127.0.0.1:8554/{stream}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode == 0:
        raise RuntimeError(f"unauthorized publisher reached {stream}")


def _wait_for_path(stream: str, credential: tuple[str, str]) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            payload = _json_request("http://127.0.0.1:9997/v3/paths/list", credential)
            for item in payload.get("items", []):
                if item.get("name") == stream and item.get("ready") is True:
                    return item
        except (HTTPError, URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{stream} did not become ready")


def _wait_for_api(credential: tuple[str, str]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            _json_request("http://127.0.0.1:9997/v3/paths/list", credential)
            return
        except (HTTPError, URLError):
            time.sleep(0.1)
    raise RuntimeError("MediaMTX API did not become ready")


def _wait_for_path_unready(stream: str, credential: tuple[str, str]) -> dict[str, Any] | None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        payload = _json_request("http://127.0.0.1:9997/v3/paths/list", credential)
        item = next((item for item in payload.get("items", []) if item.get("name") == stream), None)
        if item is None or item.get("ready") is not True:
            return item
        time.sleep(0.1)
    raise RuntimeError(f"{stream} remained ready after its publisher stopped")


def _wait_for_path_progress(
    stream: str,
    credential: tuple[str, str],
    *,
    prior_bytes: object,
) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        item = _wait_for_path(stream, credential)
        received = item.get("bytesReceived")
        if isinstance(received, int) and received > 0 and received != prior_bytes:
            return item
        time.sleep(0.1)
    raise RuntimeError(f"{stream} did not resume inbound byte progress")


def _wait_for_hls_decode(stream: str, credential: tuple[str, str]) -> dict[str, object]:
    url = f"http://127.0.0.1:8888/{stream}/index.m3u8"
    authorization = base64.b64encode(f"{credential[0]}:{credential[1]}".encode()).decode()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-headers",
                    f"Authorization: Basic {authorization}\r\n",
                    "-i",
                    url,
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            return {"codec": "h264", "frames": 1}
        time.sleep(0.1)
    raise RuntimeError(f"{stream} HLS fallback did not yield a decodable frame")


def _run_browser_acceptance(reader_credential: tuple[str, str]) -> dict[str, object]:
    environment = {
        **os.environ,
        "SWEEP_MEDIA_WEBRTC_ORIGIN": "http://localhost:8889",
        "SWEEP_MEDIA_HLS_ORIGIN": "http://localhost:8888",
        "SWEEP_MEDIA_READ_USERNAME": reader_credential[0],
        "SWEEP_MEDIA_READ_PASSWORD": reader_credential[1],
    }
    server = subprocess.Popen(
        [
            "console/node_modules/.bin/vite",
            "--host",
            "localhost",
            "--port",
            "5173",
            "--strictPort",
            "console",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        _wait_for_console()
        result = subprocess.run(
            ["node", "console/scripts/media-browser-smoke.mjs"],
            check=False,
            capture_output=True,
            text=True,
            timeout=50,
            env=environment,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        payload = json.loads(result.stdout.splitlines()[-1])
        if payload.get("event") != "browser_playback_rendered":
            raise RuntimeError("browser smoke did not report rendered WHEP and HLS frames")
        return payload
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _wait_for_console() -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            _request("http://localhost:5173/?fixture=control", None)
            return
        except URLError:
            time.sleep(0.1)
    raise RuntimeError("console development server did not become ready")


def _wait_for_recording(stream: str, existing: set[Path]) -> Path:
    deadline = time.monotonic() + 8
    root = Path("recordings") / stream
    while time.monotonic() < deadline:
        recordings = [
            path for path in root.glob("*.mp4") if path not in existing and path.stat().st_size > 0
        ]
        if recordings:
            return max(recordings, key=lambda path: path.stat().st_mtime_ns)
        time.sleep(0.1)
    raise RuntimeError(f"{stream} recording did not finalize")


def _probe_recording(recording: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,duration",
            "-of",
            "json",
            str(recording),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams or streams[0].get("codec_name") != "h264":
        raise RuntimeError(f"{recording} did not contain a decodable H.264 video stream")
    stream = streams[0]
    return {
        "codec": stream["codec_name"],
        "width": stream.get("width"),
        "height": stream.get("height"),
    }


def _wait_for_retention_deletion(stream: str, existing: set[Path], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    root = Path("recordings") / stream
    while time.monotonic() < deadline:
        created = [path for path in root.glob("*.mp4") if path not in existing]
        if not created:
            return
        time.sleep(0.2)
    raise RuntimeError(f"{stream} recordings exceeded the configured retention interval")


def _expect_http_status(
    url: str,
    credential: tuple[str, str] | None,
    *,
    expected: tuple[int, ...],
    method: str = "GET",
) -> None:
    try:
        _request(url, credential, method=method)
        status = 200
    except HTTPError as error:
        status = error.code
    if status not in expected:
        raise RuntimeError(f"{method} {url} returned {status}, expected {expected}")


def _json_request(url: str, credential: tuple[str, str]) -> dict[str, Any]:
    return json.loads(_request(url, credential))


def _request(
    url: str,
    credential: tuple[str, str] | None,
    *,
    method: str = "GET",
) -> bytes:
    request = Request(url, method=method)
    if credential is not None:
        raw = f"{credential[0]}:{credential[1]}".encode()
        request.add_header("Authorization", f"Basic {base64.b64encode(raw).decode()}")
    with urlopen(request, timeout=2) as response:
        return response.read()


def _epoch_ms() -> int:
    return time.time_ns() // 1_000_000


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
