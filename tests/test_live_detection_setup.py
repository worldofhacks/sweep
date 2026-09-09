import hashlib
import json
import stat
from urllib.parse import unquote, urlsplit

import pytest

from tools import live_detection_setup as setup


@pytest.fixture
def runtime():
    return {
        "SWEEP_RELAY_TOKEN": "r" * 32,
        "SWEEP_ADAPTER_KEYS_JSON": json.dumps({"11": "a" * 32}),
        "SWEEP_NODE_TYPES_JSON": '{"11":"ground"}',
        "SWEEP_MEDIA_CAMERAS_JSON": json.dumps(
            {
                "11": [
                    {"camera_id": "head", "label": "Head", "stream": "ground1"},
                    {"camera_id": "rear", "label": "Rear", "stream": "ground1-rear"},
                ]
            }
        ),
        "SWEEP_MEDIA_READ_USERNAME": "sweep-reader",
        "SWEEP_MEDIA_READ_PASSWORD": "password/with:@special?characters",
    }


def test_explicit_cameras_and_reader_credentials(runtime):
    sources = setup.source_records(runtime, ["11:rear:1280x720"], "rtsp://127.0.0.1:8554")
    assert sources[0]["camera_id"] == "rear"
    assert sources[0]["stream"] == "ground1-rear"
    url = urlsplit(sources[0]["stream_url"])
    assert url.path == "/ground1-rear"
    assert unquote(url.password) == runtime["SWEEP_MEDIA_READ_PASSWORD"]
    assert runtime["SWEEP_RELAY_TOKEN"] not in sources[0]["stream_url"]


@pytest.mark.parametrize(
    "cameras",
    [
        ["11:primary:1280x720"],
        ["1:head:1280x720"],
        ["11:head:1280x720", "11:head:1280x720"],
        ["11:head:3840x2160"],
        ["011:head:1280x720"],
    ],
)
def test_no_invented_or_duplicate_camera_bindings(runtime, cameras):
    with pytest.raises(ValueError):
        setup.source_records(runtime, cameras, "rtsp://127.0.0.1:8554")


def test_prepare_private_config_and_preserve_existing(runtime, tmp_path, monkeypatch):
    model = tmp_path / "source.onnx"
    model.write_bytes(b"test pinned model")
    monkeypatch.setattr(
        setup, "YOLOX_S_ONNX_SHA256", hashlib.sha256(model.read_bytes()).hexdigest()
    )
    output = tmp_path / "detection"
    config = setup.prepare(runtime, ["11:head:1280x720"], "rtsp://127.0.0.1:8554", output, model)
    before = config.read_bytes()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        setup.prepare(runtime, ["11:head:1280x720"], "rtsp://127.0.0.1:8554", output, model)
    assert config.read_bytes() == before


def test_invalid_model_removes_only_new_output(runtime, tmp_path):
    model = tmp_path / "wrong.onnx"
    model.write_bytes(b"not the pinned model")
    output = tmp_path / "detection"
    with pytest.raises(ValueError, match="pinned"):
        setup.prepare(runtime, ["11:head:1280x720"], "rtsp://127.0.0.1:8554", output, model)
    assert not output.exists()
    assert model.read_bytes() == b"not the pinned model"


def test_probe_requires_distinct_frames_and_closes_workers(runtime, tmp_path, monkeypatch):
    model = tmp_path / "source.onnx"
    model.write_bytes(b"test pinned model")
    monkeypatch.setattr(
        setup, "YOLOX_S_ONNX_SHA256", hashlib.sha256(model.read_bytes()).hexdigest()
    )
    config = setup.prepare(
        runtime, ["11:head:1280x720"], "rtsp://127.0.0.1:8554", tmp_path / "detection", model
    )
    workers = []

    class Service:
        def __init__(self, sources):
            self.closed, self.calls = False, 0
            workers.append(self)

        def snapshot(self, *args):
            self.calls += 1
            return {
                "state": "live",
                "frame": {"sequence": max(1, self.calls - 1), "detections": []},
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(setup.time, "sleep", lambda _: None)
    report = setup.check(runtime, config, service_factory=Service)
    assert report["passed"]
    assert report["cameras"][0]["fresh_frames"] == 2
    assert workers[0].calls == 3 and workers[0].closed
    assert runtime["SWEEP_MEDIA_READ_PASSWORD"] not in json.dumps(report)


def test_cli_failure_does_not_disclose_private_runtime(tmp_path, capsys):
    path = tmp_path / "runtime.json"
    path.write_text('{"SECRET":"never echo this credential"}')
    path.chmod(0o600)
    assert (
        setup.main(
            [
                "--runtime-json",
                str(path),
                "prepare",
                "--camera",
                "11:head:1280x720",
                "--output",
                str(tmp_path / "output"),
                "--download-model",
            ]
        )
        == 2
    )
    assert "never echo" not in capsys.readouterr().out
