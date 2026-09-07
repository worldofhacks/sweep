"""Prepare a hash-pinned Telebot owner patch without touching a device."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

CRLF_REFERENCE_SHA256 = "f463feaab912999d3b4133fea049925ed95a6e32ee82856fb7ffa273cbe2ca3e"
LF_REFERENCE_SHA256 = "e128a740200b7f8d538414c8963109f1ee2f0475b340f9369814ba8446891300"
_REPRESENTATIONS = {
    CRLF_REFERENCE_SHA256: b"\r\n",
    LF_REFERENCE_SHA256: b"\n",
}


def _lines(newline: bytes, *lines: bytes) -> bytes:
    return newline.join(lines) + newline


def _anchors(newline: bytes) -> tuple[bytes, bytes, bytes, bytes, bytes, bytes]:
    require = _lines(newline, b"const LidarNode = require('./lidar_node');")
    serial = _lines(newline, b"  this._serial = new Serial();")
    model = _lines(
        newline,
        b"  this._model = new ControlModel({",
        b'    calibpath: config_path + "/telebot_calib.json"',
        b"  }, this._serial, this);",
    )
    require_replacement = require + _lines(
        newline, b"const PairedEncoderSampler = require('./sweep_paired_encoder_sampler');"
    )
    serial_replacement = serial + _lines(
        newline,
        b"  this._sweep_encoder = new PairedEncoderSampler(",
        b"    this._serial, config_path + '/sweep_encoder.sock'",
        b"  );",
        b"  this._sweep_encoder.start();",
    )
    model_replacement = model + _lines(
        newline,
        b"  const sweepEncoderModelStart = this._model.start.bind(this._model);",
        b"  this._model.start = function () {",
        b"    sweepEncoderModelStart();",
        b"    self._sweep_encoder.activate();",
        b"  };",
        b"  const sweepEncoderModelInitialize = this._model.initialize.bind(this._model);",
        b"  this._model.initialize = function (fw_version) {",
        b"    self._sweep_encoder.beginInitialization();",
        b"    return sweepEncoderModelInitialize(fw_version);",
        b"  };",
    )
    return require, serial, model, require_replacement, serial_replacement, model_replacement


def prepare(source: bytes) -> bytes:
    digest = hashlib.sha256(source).hexdigest()
    newline = _REPRESENTATIONS.get(digest)
    if newline is None:
        raise ValueError("vendor telebot_node.js does not match a reviewed representation")
    require, serial, model, require_replacement, serial_replacement, model_replacement = _anchors(
        newline
    )
    if source.count(require) != 1 or source.count(serial) != 1 or source.count(model) != 1:
        raise ValueError("vendor telebot_node.js does not contain the reviewed patch anchors")
    patched = (
        source.replace(require, require_replacement)
        .replace(serial, serial_replacement)
        .replace(model, model_replacement)
    )
    if (
        patched.count(b"sweep_paired_encoder_sampler") != 1
        or patched.count(b"_sweep_encoder.start()") != 1
        or patched.count(b"_sweep_encoder.activate()") != 1
        or patched.count(b"_sweep_encoder.beginInitialization()") != 1
    ):
        raise ValueError("prepared owner patch is ambiguous")
    return patched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(prepare(args.source.read_bytes()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
