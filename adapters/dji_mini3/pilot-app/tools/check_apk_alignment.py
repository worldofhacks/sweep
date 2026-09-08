#!/usr/bin/env python3
"""Check packaged 64-bit native ELF and ZIP alignment for Android 16 KB pages.

Usage: python3 tools/check_apk_alignment.py app/build/outputs/apk/probe/debug/app-probe-debug.apk
This checks binary layout, not runtime page-size assumptions or device compatibility.
"""

import argparse
import struct
from pathlib import Path
from zipfile import ZIP_STORED, BadZipFile, ZipFile

PAGE_SIZE = 16_384
ABIS = {"arm64-v8a", "x86_64"}


def check_apk(path: Path) -> tuple[int, list[str]]:
    failures: list[str] = []
    count = 0
    with ZipFile(path) as archive, path.open("rb") as raw:
        for entry in archive.infolist():
            parts = entry.filename.split("/")
            if (
                len(parts) != 3
                or parts[0] != "lib"
                or parts[1] not in ABIS
                or not parts[2].endswith(".so")
            ):
                continue
            count += 1
            data = archive.read(entry)
            if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
                failures.append(f"{entry.filename}: expected a little-endian 64-bit ELF")
                continue
            header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
            offset, size, entries = header[4], header[8], header[9]
            if size < 56 or offset + size * entries > len(data):
                failures.append(f"{entry.filename}: invalid ELF program-header table")
                continue
            loads = 0
            for index in range(entries):
                segment = struct.unpack_from("<IIQQQQQQ", data, offset + index * size)
                if segment[0] != 1:  # PT_LOAD
                    continue
                loads += 1
                file_offset, address, alignment = segment[2], segment[3], segment[7]
                if (
                    alignment < PAGE_SIZE
                    or alignment & (alignment - 1)
                    or (file_offset - address) % PAGE_SIZE
                ):
                    failures.append(
                        f"{entry.filename}: PT_LOAD[{index}] is not 16 KB aligned "
                        f"(align={alignment:#x}, offset={file_offset:#x}, vaddr={address:#x})"
                    )
            if not loads:
                failures.append(f"{entry.filename}: no ELF PT_LOAD segments")
            if entry.compress_type == ZIP_STORED:
                raw.seek(entry.header_offset)
                local = raw.read(30)
                if len(local) != 30 or local[:4] != b"PK\x03\x04":
                    failures.append(f"{entry.filename}: invalid ZIP local header")
                    continue
                name_size, extra_size = struct.unpack_from("<HH", local, 26)
                payload_offset = entry.header_offset + 30 + name_size + extra_size
                if payload_offset % PAGE_SIZE:
                    failures.append(
                        f"{entry.filename}: uncompressed ZIP payload is not 16 KB aligned"
                    )
    if not count:
        failures.append("no packaged 64-bit native libraries found")
    return count, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    args = parser.parse_args()
    try:
        count, failures = check_apk(args.apk)
    except (OSError, BadZipFile, struct.error) as error:
        parser.exit(1, f"APK check failed: {error}\n")
    for failure in failures:
        print(f"FAIL {failure}")
    print(f"{args.apk.name}: {count} native libraries, {len(failures)} alignment failures")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
