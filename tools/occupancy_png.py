from __future__ import annotations

import struct
import zlib


def decode_occupancy_png(payload: bytes, *, max_pixels: int = 262_144) -> tuple[bytes, ...]:
    """Decode bounded grayscale rows; damaged or unsupported PNG input raises ValueError."""
    if len(payload) > 8 * 1024 * 1024 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("ground occupancy requires a bounded PNG")
    offset = 8
    width = height = 0
    compressed = bytearray()
    image_started = image_ended = finished = False
    for _ in range(4096):
        if offset + 12 > len(payload):
            raise ValueError("truncated occupancy PNG")
        length = int.from_bytes(payload[offset : offset + 4], "big")
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ValueError("truncated occupancy PNG chunk")
        data = payload[offset + 8 : end - 4]
        crc = int.from_bytes(payload[end - 4 : end], "big")
        if zlib.crc32(kind + data) != crc:
            raise ValueError("occupancy PNG checksum mismatch")
        if offset == 8:
            if kind != b"IHDR" or len(data) != 13:
                raise ValueError("occupancy PNG lacks its header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if (
                not 0 < width <= max_pixels
                or not 0 < height <= max_pixels
                or width * height > max_pixels
                or (depth, color, compression, filtering, interlace) != (8, 0, 0, 0, 0)
            ):
                raise ValueError("ground occupancy requires bounded non-interlaced 8-bit grayscale")
        elif kind == b"IDAT":
            if image_ended:
                raise ValueError("occupancy PNG image chunks are not contiguous")
            image_started = True
            compressed.extend(data)
        elif kind == b"IEND":
            if data or not image_started or end != len(payload):
                raise ValueError("invalid occupancy PNG end")
            finished = True
            break
        elif kind in {b"tRNS", b"acTL", b"fcTL", b"fdAT"} or not kind[0] & 32:
            raise ValueError("unsupported occupancy PNG chunk")
        elif image_started:
            image_ended = True
        offset = end
    if not finished:
        raise ValueError("occupancy PNG exceeds its chunk bound")
    expected = height * (width + 1)
    try:
        decoder = zlib.decompressobj()
        filtered = decoder.decompress(compressed, expected + 1)
    except zlib.error as error:
        raise ValueError("invalid occupancy PNG compression") from error
    if (
        len(filtered) != expected
        or not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
    ):
        raise ValueError("occupancy PNG decompression exceeds its exact image bound")
    rows = []
    previous = bytes(width)
    for start in range(0, len(filtered), width + 1):
        method = filtered[start]
        if method > 4:
            raise ValueError("unsupported occupancy PNG filter")
        row = bytearray(filtered[start + 1 : start + 1 + width])
        for column in range(width):
            left = row[column - 1] if column else 0
            above = previous[column]
            upper_left = previous[column - 1] if column else 0
            if method == 0:
                prediction = 0
            elif method == 1:
                prediction = left
            elif method == 2:
                prediction = above
            elif method == 3:
                prediction = (left + above) // 2
            else:
                estimate = left + above - upper_left
                distances = (
                    abs(estimate - left),
                    abs(estimate - above),
                    abs(estimate - upper_left),
                )
                prediction = (left, above, upper_left)[distances.index(min(distances))]
            row[column] = (row[column] + prediction) % 256
        previous = bytes(row)
        rows.append(previous)
    return tuple(rows)
