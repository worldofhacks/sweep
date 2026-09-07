"""Host contracts; no robot, relay, simulator, or operator fixture UI is started."""

import functools
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tools import console
from tools.console import ConsoleHandler, ThreadingHTTPServer, bootstrap


class ConsoleHostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.release = Path(self.directory.name)
        (self.release / "dist").mkdir()
        (self.release / "dist" / "index.html").write_text("<title>Host contract</title>")
        (self.release / "runtime.json").write_text("private-file-must-not-be-served")
        self.environment = {
            "SWEEP_RELAY_ORIGIN": "ws://relay.test:8010",
            "SWEEP_SESSION_ID": "contract-session",
            "SWEEP_RELAY_TOKEN": "test-only-token",
            "SWEEP_MEDIA_WEBRTC_ORIGIN": "http://media.test:8889",
            "SWEEP_MEDIA_READ_USERNAME": "test-reader",
            "SWEEP_MEDIA_READ_PASSWORD": "test-only-password",
        }
        self.version = {"application": "sweep-console", "source_revision": "test-revision"}
        handler = functools.partial(
            ConsoleHandler, release=self.release, environment=self.environment, version=self.version
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def get(self, route):
        return urlopen(self.url + route, timeout=2)

    def test_real_bootstrap_is_separate_from_static_assets_and_version(self):
        with self.get("/relay-bootstrap.json") as response:
            self.assertEqual(
                json.load(response)["relay"],
                {
                    "baseUrl": self.environment["SWEEP_RELAY_ORIGIN"],
                    "sessionId": "contract-session",
                    "token": "test-only-token",
                },
            )
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        for route in ("/", "/console-version.json"):
            with self.get(route) as response:
                self.assertNotIn("test-only-token", response.read().decode())
        with self.get("/runtime-config.json") as response:
            self.assertEqual(json.load(response)["media"]["webrtcOrigin"], "http://media.test:8889")

    def test_missing_configuration_stays_unavailable(self):
        self.environment.clear()
        for route, field in (("/relay-bootstrap.json", "relay"), ("/runtime-config.json", "media")):
            with self.assertRaises(HTTPError) as caught:
                self.get(route)
            self.assertEqual(caught.exception.code, 503)
            self.assertIsNone(json.load(caught.exception)[field])
        self.assertIsNone(bootstrap({"SWEEP_RELAY_TOKEN": "token-without-real-session"}))
        self.assertIsNone(
            bootstrap(
                {
                    "SWEEP_RELAY_TOKEN": "token",
                    "SWEEP_SESSION_ID": "s",
                    "SWEEP_RELAY_ORIGIN": "file:///private",
                }
            )
        )

    def test_private_files_and_symlinks_are_not_served(self):
        (self.release / "dist" / "outside").symlink_to(self.release / "runtime.json")
        for route in ("/runtime.json", "/../runtime.json", "/outside", "/src/main.tsx"):
            with self.assertRaises(HTTPError) as caught:
                self.get(route)
            self.assertEqual(caught.exception.code, 404)

    def test_second_instance_cannot_take_an_occupied_port(self):
        with self.assertRaises(OSError):
            ThreadingHTTPServer(self.server.server_address, ConsoleHandler)

    def test_head_uses_same_runtime_contract_without_response_body(self):
        with urlopen(
            Request(self.url + "/console-version.json", method="HEAD"), timeout=2
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            self.assertGreater(int(response.headers["Content-Length"]), 0)

    def test_invalid_origins_and_empty_or_mistyped_configuration_stay_unavailable(self):
        original = self.environment.copy()
        invalid = [
            "ws://",
            "ws://[malformed",
            "ws://host:invalid",
            "ws://host:0",
            "ws://user:fake-password@host",
            "ws://host/?token=fake-secret",
            "ws://host/#fragment",
            " ws://host",
            "ws://ho\nst",
            "ws://host:65536",
        ]
        for origin in invalid:
            with self.subTest(origin=origin):
                self.environment["SWEEP_RELAY_ORIGIN"] = origin
                with self.assertRaises(HTTPError) as caught:
                    self.get("/relay-bootstrap.json")
                self.assertEqual(caught.exception.code, 503)
                self.assertIsNone(json.load(caught.exception)["relay"])
        self.environment.update(original)
        for origin in [
            "http://",
            "https://[broken",
            "http://host/path",
            "file:///private",
            "http://user:fake-password@host",
            "http://host/?credential=fake-secret",
            1,
        ]:
            self.environment["SWEEP_MEDIA_WEBRTC_ORIGIN"] = origin
            with self.assertRaises(HTTPError) as caught:
                self.get("/runtime-config.json")
            self.assertEqual(caught.exception.code, 503)
        for key in ("SWEEP_SESSION_ID", "SWEEP_RELAY_TOKEN"):
            for value in ("   ", 1, None):
                self.assertIsNone(bootstrap({**original, key: value}))
        self.assertIsNone(bootstrap({**original, "SWEEP_SESSION_ID": " padded "}))


class ConsoleReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / ".sweep" / "console"
        self.state.mkdir(parents=True)
        self.dist = self.root / "console" / "dist"
        self.dist.mkdir(parents=True)
        (self.dist / "index.html").write_text("<title>Immutable release</title>")
        for name, value in (("ROOT", self.root), ("STATE", self.state)):
            self.enterContext(patch.object(console, name, value))
        self.enterContext(patch.object(console.subprocess, "run"))
        self.enterContext(
            patch.object(
                console.subprocess,
                "check_output",
                side_effect=lambda args, **_: "a" * 40 if args[1] == "rev-parse" else b"",
            )
        )

    def test_interrupted_copy_is_never_published_and_retry_copies_the_complete_release(self):
        with patch.object(console.shutil, "copytree", side_effect=OSError("interrupted copy")):
            with self.assertRaises(OSError):
                console.build()
        self.assertFalse((self.state / "active-build.json").exists())
        self.assertEqual(list((self.state / "releases").iterdir()), [])
        metadata = console.build()
        self.assertEqual(
            (Path(metadata["release"]) / "dist/index.html").read_bytes(),
            (self.dist / "index.html").read_bytes(),
        )

    def test_repeated_build_reuses_immutable_metadata_and_detects_asset_corruption(self):
        first = console.build()
        version = Path(first["release"]) / "version.json"
        original = version.read_bytes()
        self.assertEqual(console.build(), first)
        self.assertEqual(version.read_bytes(), original)
        (Path(first["release"]) / "dist/index.html").write_text("corrupted")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            console.build()
        self.assertEqual(console.read_json(self.state / "active-build.json"), first)
        with patch.object(console, "ThreadingHTTPServer") as server:
            with self.assertRaisesRegex(RuntimeError, "integrity"):
                console.serve()
            server.assert_not_called()

    def test_changed_assets_create_a_new_release_without_changing_the_old_one(self):
        first = console.build()
        first_index = Path(first["release"]) / "dist/index.html"
        original = first_index.read_bytes()
        (self.dist / "index.html").write_text("new artifact")
        second = console.build()
        self.assertNotEqual(first["build_id"], second["build_id"])
        self.assertEqual(first_index.read_bytes(), original)

    def test_build_symlinks_cannot_copy_private_files_into_static_release(self):
        private = self.root / "private.json"
        private.write_text("test-only-private-content")
        (self.dist / "outside").symlink_to(private)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            console.build()
        self.assertFalse((self.state / "active-build.json").exists())

    def test_stale_pid_record_never_signals_a_reused_process(self):
        record = self.state / "process.json"
        console.write_json(record, {"pid": 123, "process_start": "old identity", "instance": "old"})
        with (
            patch.object(console, "process_identity", return_value="new identity"),
            patch.object(console.os, "kill") as kill,
        ):
            console.stop()
            kill.assert_not_called()
        self.assertFalse(record.exists())

    def test_missing_process_identity_cannot_authorize_a_stop(self):
        record = self.state / "process.json"
        console.write_json(record, {"pid": 123, "process_start": "", "instance": "old"})
        with (
            patch.object(console, "process_identity", return_value=""),
            patch.object(console.os, "kill") as kill,
        ):
            with self.assertRaisesRegex(RuntimeError, "record is invalid"):
                console.stop()
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
