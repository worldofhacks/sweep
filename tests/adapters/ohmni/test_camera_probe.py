"""Safety-boundary tests for the Ohmni camera probe process."""

import pytest

from adapters.ohmni.spike import camera_probe


@pytest.mark.parametrize(
    "argv",
    [
        ["--seconds", "0"],
        ["--seconds", "-1"],
        ["--seconds", "301"],
        ["--seconds", "nan"],
        ["--seconds", "inf"],
        ["--warmup", "0"],
        ["--warmup", "31"],
        ["--warmup", "nan"],
        ["--publish", "http://camera.example/stream"],
        ["--publish", "rtsp:///missing-host"],
        ["--publish", "rtsp://camera.example:not-a-port/live"],
        ["--publish", "rtsp://" + "x" * camera_probe.MAX_PUBLISH_URL_BYTES],
        ["--seconds", "2", "--warmup", "2", "--publish", "rtsp://camera.example/live"],
    ],
)
def test_camera_probe_refuses_unbounded_or_invalid_runs(argv):
    args = camera_probe.build_parser().parse_args(argv)
    with pytest.raises(ValueError):
        camera_probe.validate_args(args)


def test_camera_probe_redacts_rtsp_userinfo_query_and_fragment():
    url = "rtsp://alice:secret@[::1]:8554/ground1?token=also-secret#private"
    redacted = camera_probe.redact_publish_url(url)
    assert redacted.startswith("rtsp://<credentials>@[::1]:8554/ground1?")
    for secret in ("alice", "secret", "token", "also-secret", "private"):
        assert secret not in redacted
    assert camera_probe.redact_publish_url("rtsp://camera.example/ground1") == (
        "rtsp://camera.example/ground1"
    )
    query_redacted = camera_probe.redact_publish_url("rtsp://camera.example/ground1?token=secret")
    assert "token" not in query_redacted
    assert "secret" not in query_redacted
    assert "secret" not in camera_probe.redact_publish_url("rtsp://camera.example/ground1#secret")


def test_run_validates_before_touching_the_socket():
    args = camera_probe.build_parser().parse_args(["--seconds", "inf"])
    with pytest.raises(ValueError):
        camera_probe.run(args)


def test_raw_output_is_exclusive_and_byte_bounded(monkeypatch, tmp_path):
    existing = tmp_path / "existing.raw"
    existing.write_bytes(b"prior")
    with pytest.raises(FileExistsError):
        camera_probe.RawOutput(str(existing))
    assert existing.read_bytes() == b"prior"

    path = tmp_path / "bounded.raw"
    monkeypatch.setattr(camera_probe, "MAX_RAW_OUTPUT_BYTES", 4)
    output = camera_probe.RawOutput(str(path))
    try:
        output.write(b"1234")
        with pytest.raises(camera_probe.ProbeError, match="raw output exceeds"):
            output.write(b"5")
    finally:
        output.close()
    assert path.read_bytes() == b"1234"


def test_bind_stream_refuses_to_unlink_a_non_socket(tmp_path):
    path = tmp_path / "camera.sock"
    path.write_text("keep me")
    with pytest.raises(camera_probe.ProbeError, match="refusing to remove non-socket"):
        camera_probe.bind_stream(str(path))
    assert path.read_text() == "keep me"


def test_bind_and_cleanup_remove_only_the_created_socket(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "camera.sock"
    monkeypatch.setattr(camera_probe.os, "chown", lambda *args: None)
    server, identity = camera_probe.bind_stream("camera.sock")
    try:
        assert path.exists()
    finally:
        server.close()
    assert camera_probe.remove_bound_stream("camera.sock", identity)
    assert not path.exists()


def test_cleanup_refuses_to_unlink_a_replaced_non_socket(tmp_path):
    path = tmp_path / "camera.sock"
    path.write_text("replacement")
    assert not camera_probe.remove_bound_stream(str(path), (1, 2))
    assert path.read_text() == "replacement"
