from __future__ import annotations

import hashlib

import pytest

from .tools.prepare_owner_encoder_patch import prepare


def _source() -> bytes:
    return (
        b"const LidarNode = require('./lidar_node');\r\n"
        b"  this._serial = new Serial();\r\n"
        b"  this._model = new ControlModel();\r\n"
        b"  this._api = new LocalApi(this, config_path);\r\n"
    )


def test_owner_patch_is_hash_pinned_and_inserts_each_anchor_once() -> None:
    source = _source()
    patched = prepare(source, hashlib.sha256(source).hexdigest())
    assert patched.count(b"sweep_paired_encoder_sampler") == 1
    assert patched.count(b"_sweep_encoder.start()") == 1
    assert patched.index(b"_sweep_encoder.start()") < patched.index(b"new ControlModel")


@pytest.mark.parametrize(
    "source", [_source() + b"extra", _source().replace(b"LocalApi", b"OtherApi")]
)
def test_owner_patch_refuses_unknown_or_ambiguous_vendor_source(source: bytes) -> None:
    with pytest.raises(ValueError):
        prepare(source, hashlib.sha256(_source()).hexdigest())
