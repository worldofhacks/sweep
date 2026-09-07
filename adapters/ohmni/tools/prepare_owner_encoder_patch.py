"""Prepare a hash-pinned Telebot owner patch without touching a device."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

REFERENCE_SHA256 = "f463feaab912999d3b4133fea049925ed95a6e32ee82856fb7ffa273cbe2ca3e"
_REQUIRE = b"const LidarNode = require('./lidar_node');\r\n"
_REQUIRE_REPLACEMENT = _REQUIRE + (
    b"const PairedEncoderSampler = require('./sweep_paired_encoder_sampler');\r\n"
)
_SERIAL = b"  this._serial = new Serial();\r\n"
_SERIAL_REPLACEMENT = _SERIAL + (
    b"  this._sweep_encoder = new PairedEncoderSampler(\r\n"
    b"    this._serial, config_path + '/sweep_encoder.sock'\r\n"
    b"  );\r\n"
    b"  this._sweep_encoder.start();\r\n"
)
_MODEL = (
    b"  this._model = new ControlModel({\r\n"
    b'    calibpath: config_path + "/telebot_calib.json"\r\n'
    b"  }, this._serial, this);\r\n"
)
_MODEL_REPLACEMENT = _MODEL + (
    b"  const sweepEncoderModelStart = this._model.start.bind(this._model);\r\n"
    b"  this._model.start = function () {\r\n"
    b"    sweepEncoderModelStart();\r\n"
    b"    self._sweep_encoder.activate();\r\n"
    b"  };\r\n"
    b"  const sweepEncoderModelInitialize = this._model.initialize.bind(this._model);\r\n"
    b"  this._model.initialize = function (fw_version) {\r\n"
    b"    self._sweep_encoder.beginInitialization();\r\n"
    b"    return sweepEncoderModelInitialize(fw_version);\r\n"
    b"  };\r\n"
)


def prepare(source: bytes, expected_sha256: str = REFERENCE_SHA256) -> bytes:
    if hashlib.sha256(source).hexdigest() != expected_sha256:
        raise ValueError("vendor telebot_node.js does not match the reviewed source")
    if source.count(_REQUIRE) != 1 or source.count(_SERIAL) != 1 or source.count(_MODEL) != 1:
        raise ValueError("vendor telebot_node.js does not contain the reviewed patch anchors")
    patched = (
        source.replace(_REQUIRE, _REQUIRE_REPLACEMENT)
        .replace(_SERIAL, _SERIAL_REPLACEMENT)
        .replace(_MODEL, _MODEL_REPLACEMENT)
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
    output = prepare(args.source.read_bytes())
    args.output.write_bytes(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
