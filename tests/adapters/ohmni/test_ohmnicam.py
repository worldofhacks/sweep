"""Pure-function tests for OHMNICAM header parsing and frame reassembly."""

from adapters.ohmni.spike import ohmnicam as oc


def test_frame_start_header_roundtrip():
    datagram = oc.encode_frame_start(640, 480, 3, 640 * 480)
    assert len(datagram) == oc.FRAME_START_LEN
    assert datagram[:8] == b"OHMNICAM"
    assert oc.parse_control(datagram) == (oc.MSG_FRAME_START, oc.FrameHeader(640, 480, 3, 307200))
    assert "640" in repr(oc.FrameHeader(640, 480, 3, 307200))


def test_parse_control_separates_data_chunks_from_control():
    assert oc.parse_control(b"x" * 100) is None
    assert oc.parse_control(b"OHMNICAM\x01\x00\x00\x00" + b"\x00" * 60) is None
    assert oc.parse_control(b"NOTMAGIC\x01\x00\x00\x00") is None
    assert oc.parse_control(b"OHMNICAM\x01\x00") is None
    assert oc.parse_control(oc.encode_frame_end()) == (oc.MSG_FRAME_END, None)
    assert oc.parse_control(b"OHMNICAM\x01\x00\x00\x00") == (oc.MSG_FRAME_START, None)


def test_frame_reassembly_across_datagrams():
    payload = bytes(range(256))
    assembler = oc.FrameAssembler()
    assert assembler.feed(b"\x00" * 200) is None
    assert assembler.ignored == 1
    assert assembler.feed(oc.encode_frame_start(32, 8, 0, len(payload))) is None
    assert assembler.filling
    assert assembler.feed(payload[:100]) is None
    assert assembler.feed(payload[100:200]) is None
    frame = assembler.feed(payload[200:])
    assert frame is not None
    assert frame.data == payload
    assert frame.header == oc.FrameHeader(32, 8, 0, 256)
    assert not assembler.filling
    assert assembler.feed(oc.encode_frame_end()) is None
    assert (assembler.frames, assembler.dropped) == (1, 0)


def test_frame_start_while_filling_drops_the_partial_frame():
    assembler = oc.FrameAssembler()
    assembler.feed(oc.encode_frame_start(4, 4, 0, 16))
    assembler.feed(b"\x01" * 8)
    assert assembler.feed(oc.encode_frame_start(4, 4, 0, 16)) is None
    assert assembler.dropped == 1
    frame = assembler.feed(b"\x02" * 16)
    assert frame.data == b"\x02" * 16
    assert assembler.frames == 1


def test_frame_end_while_filling_drops_and_returns_to_searching():
    assembler = oc.FrameAssembler()
    assembler.feed(oc.encode_frame_start(4, 4, 0, 16))
    assembler.feed(b"\x01" * 8)
    assert assembler.feed(oc.encode_frame_end()) is None
    assert assembler.dropped == 1
    assert not assembler.filling
    assert assembler.feed(b"\x01" * 8) is None
    assert assembler.ignored == 1


def test_oversized_final_chunk_is_truncated_to_the_announced_size():
    assembler = oc.FrameAssembler()
    assembler.feed(oc.encode_frame_start(5, 2, 0, 10))
    frame = assembler.feed(b"\x07" * 12)
    assert len(frame.data) == 10


def test_short_frame_start_is_counted_as_malformed():
    assembler = oc.FrameAssembler()
    assert assembler.feed(b"OHMNICAM\x01\x00\x00\x00") is None
    assert assembler.malformed == 1
    assert not assembler.filling
