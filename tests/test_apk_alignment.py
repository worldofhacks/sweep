"""Exercise Android package layout checks without executing any native code."""

import importlib.util
import struct
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

CHECKER = (
    Path(__file__).resolve().parents[1]
    / "adapters/dji_mini3/pilot-app/tools/check_apk_alignment.py"
)
SPEC = importlib.util.spec_from_file_location("check_apk_alignment", CHECKER)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def native_elf(*, alignment=16384, address=0, segment_type=1):
    """One little-endian ELF64 program header with a tiny loadable segment."""
    identity = b"\x7fELF\x02\x01\x01".ljust(16, b"\0")
    header = struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", segment_type, 5, 0, address, 0, 120, 120, alignment)
    return identity + header + segment


def write_apk(tmp_path, libraries, *, compressed=True, align_zip=False):
    path = tmp_path / "fixture.apk"
    with ZipFile(path, "w") as archive:
        for name, binary in libraries.items():
            entry = ZipInfo(name)
            entry.compress_type = ZIP_DEFLATED if compressed else ZIP_STORED
            if align_zip:
                payload = archive.fp.tell() + 30 + len(name.encode("utf-8"))
                padding = (-payload) % 16384
                if padding:
                    if padding < 4:
                        padding += 16384
                    entry.extra = struct.pack("<HH", 0xFFFF, padding - 4) + bytes(padding - 4)
            archive.writestr(entry, binary)
    return path


@pytest.mark.parametrize("alignment", [16384, 65536])
def test_compressed_64_bit_libraries_accept_compatible_elf(tmp_path, alignment):
    apk = write_apk(
        tmp_path,
        {
            "lib/arm64-v8a/libone.so": native_elf(alignment=alignment),
            "lib/x86_64/libtwo.so": native_elf(alignment=alignment),
            "lib/armeabi-v7a/libignored.so": b"32-bit library outside this check",
        },
    )
    assert checker.check_apk(apk) == (2, [])


@pytest.mark.parametrize("alignment", [0, 1, 4096, 8192, 24576])
def test_old_webrtc_and_invalid_load_alignment_fail_even_when_compressed(tmp_path, alignment):
    apk = write_apk(tmp_path, {"lib/arm64-v8a/libjingle.so": native_elf(alignment=alignment)})
    count, failures = checker.check_apk(apk)
    assert count == 1
    assert len(failures) == 1
    assert "PT_LOAD[0] is not 16 KB aligned" in failures[0]


def test_large_alignment_does_not_hide_noncongruent_load_address(tmp_path):
    apk = write_apk(tmp_path, {"lib/arm64-v8a/libbad.so": native_elf(address=4096)})
    _, failures = checker.check_apk(apk)
    assert len(failures) == 1
    assert "PT_LOAD[0] is not 16 KB aligned" in failures[0]


def test_elf_without_load_segment_fails(tmp_path):
    apk = write_apk(tmp_path, {"lib/arm64-v8a/libbad.so": native_elf(segment_type=4)})
    assert checker.check_apk(apk) == (1, ["lib/arm64-v8a/libbad.so: no ELF PT_LOAD segments"])


@pytest.mark.parametrize("mutation", ["not_elf", "truncated", "table_outside", "short_entry"])
def test_malformed_native_libraries_fail(tmp_path, mutation):
    binary = bytearray(native_elf())
    if mutation == "not_elf":
        binary[:4] = b"nope"
    elif mutation == "truncated":
        binary = binary[:32]
    elif mutation == "table_outside":
        struct.pack_into("<Q", binary, 32, len(binary) + 1)
    else:
        struct.pack_into("<H", binary, 54, 8)
    apk = write_apk(tmp_path, {"lib/arm64-v8a/libbad.so": binary})
    count, failures = checker.check_apk(apk)
    assert count == 1
    assert len(failures) == 1
    assert "expected a little-endian 64-bit ELF" in failures[0] or "invalid ELF" in failures[0]


@pytest.mark.parametrize("align_zip", [False, True])
def test_uncompressed_library_also_requires_aligned_zip_payload(tmp_path, align_zip):
    apk = write_apk(
        tmp_path,
        {"lib/arm64-v8a/libone.so": native_elf()},
        compressed=False,
        align_zip=align_zip,
    )
    count, failures = checker.check_apk(apk)
    assert count == 1
    if align_zip:
        assert failures == []
    else:
        assert failures == [
            "lib/arm64-v8a/libone.so: uncompressed ZIP payload is not 16 KB aligned"
        ]


def test_missing_64_bit_libraries_is_not_a_passing_package(tmp_path):
    apk = write_apk(tmp_path, {"lib/armeabi-v7a/libone.so": b"32-bit"})
    assert checker.check_apk(apk) == (0, ["no packaged 64-bit native libraries found"])


@pytest.mark.parametrize("alignment,return_code", [(16384, 0), (4096, 1)])
def test_cli_exit_status_gates_package_alignment(tmp_path, alignment, return_code):
    apk = write_apk(tmp_path, {"lib/arm64-v8a/libone.so": native_elf(alignment=alignment)})
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(apk)], capture_output=True, text=True
    )
    assert result.returncode == return_code
    assert "1 native libraries" in result.stdout


@pytest.mark.parametrize("exists", [False, True])
def test_cli_rejects_missing_or_invalid_apk(tmp_path, exists):
    apk = tmp_path / "invalid.apk"
    if exists:
        apk.write_text("not a ZIP archive")
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(apk)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "APK check failed:" in result.stderr
