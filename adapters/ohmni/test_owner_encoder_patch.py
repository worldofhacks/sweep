from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from .tools.prepare_owner_encoder_patch import (
    CRLF_REFERENCE_SHA256,
    LF_REFERENCE_SHA256,
    prepare,
)

CRLF_PATCHED_SHA256 = "0394a830141bf8ce4343944b768de17887531f3c1d89e3521216b5f7ca5b82ea"
LF_PATCHED_SHA256 = "ee0a0665dc1a5931960d97032405cb4e7baf731d0cc6b08738a4d33302a5bf42"


def _lf_source() -> bytes:
    return (
        Path(__file__).with_name("vendor") / "fixtures" / "telebot_node_reviewed_lf.js"
    ).read_bytes()


def test_owner_patch_preserves_exact_reviewed_lf_representation() -> None:
    source = _lf_source()
    assert hashlib.sha256(source).hexdigest() == LF_REFERENCE_SHA256
    patched = prepare(source)
    assert hashlib.sha256(patched).hexdigest() == LF_PATCHED_SHA256
    assert b"\r\n" not in patched


def test_owner_patch_preserves_exact_reviewed_crlf_representation() -> None:
    source = _lf_source().replace(b"\n", b"\r\n")
    assert hashlib.sha256(source).hexdigest() == CRLF_REFERENCE_SHA256
    patched = prepare(source)
    assert hashlib.sha256(patched).hexdigest() == CRLF_PATCHED_SHA256
    assert b"\n" not in patched.replace(b"\r\n", b"")


@pytest.mark.parametrize(
    "source", [_lf_source() + b"extra", _lf_source().replace(b"LocalApi", b"OtherApi")]
)
def test_owner_patch_refuses_unknown_or_ambiguous_vendor_source(source: bytes) -> None:
    with pytest.raises(ValueError):
        prepare(source)
