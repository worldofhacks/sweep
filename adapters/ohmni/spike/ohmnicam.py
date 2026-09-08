"""OHMNICAM datagram parsing for the Ohmni camera stream socket ``/dev/libcamera_stream``.

While any app holds the camera, the Android HAL sends datagrams to a UNIX datagram socket
that the reader itself binds at ``/dev/libcamera_stream``. A control datagram is 12 to 64
bytes: the magic ``OHMNICAM``, a uint32 message type, and for a frame start (type 1) four
uint32 fields: width, height, format code, and frame size in bytes. Every following datagram
carries frame bytes until ``size`` bytes have arrived; type 2 marks the frame end. Frames are
grayscale (one byte per pixel, PIL mode ``L``).

Pure parsing only, so the spike probe and a later node share it. Runs on Python 3.6.
"""

import struct

MAGIC = b"OHMNICAM"
MSG_FRAME_START = 1
MSG_FRAME_END = 2
CONTROL_MIN = 12
CONTROL_MAX = 64
FRAME_START_LEN = 28

_MSGTYPE = struct.Struct("<I")
_FRAME_START = struct.Struct("<IIII")


class FrameHeader:
    """The frame-start parameters: width, height, format code, and byte size."""

    __slots__ = ("width", "height", "format", "size")

    def __init__(self, width, height, fmt, size):
        self.width = width
        self.height = height
        self.format = fmt
        self.size = size

    def __eq__(self, other):
        return isinstance(other, FrameHeader) and (
            self.width,
            self.height,
            self.format,
            self.size,
        ) == (other.width, other.height, other.format, other.size)

    def __repr__(self):
        return (
            f"FrameHeader(width={self.width}, height={self.height}, format={self.format}, "
            f"size={self.size})"
        )


class Frame:
    """A reassembled frame: its header and exactly ``header.size`` bytes."""

    __slots__ = ("header", "data")

    def __init__(self, header, data):
        self.header = header
        self.data = data


def parse_control(datagram):
    """Return ``(message_type, header_or_None)`` for a control datagram, else ``None``.

    A datagram outside 12..64 bytes or without the magic is frame data, not control. A frame
    start shorter than 28 bytes yields ``(1, None)`` so the caller can count it as malformed.
    """
    if len(datagram) < CONTROL_MIN or len(datagram) > CONTROL_MAX:
        return None
    if not datagram.startswith(MAGIC):
        return None
    msgtype = _MSGTYPE.unpack_from(datagram, len(MAGIC))[0]
    header = None
    if msgtype == MSG_FRAME_START and len(datagram) >= FRAME_START_LEN:
        width, height, fmt, size = _FRAME_START.unpack_from(datagram, CONTROL_MIN)
        header = FrameHeader(width, height, fmt, size)
    return msgtype, header


def encode_frame_start(width, height, fmt, size):
    """Inverse of ``parse_control`` for a frame start, for tests and fakes."""
    return MAGIC + _MSGTYPE.pack(MSG_FRAME_START) + _FRAME_START.pack(width, height, fmt, size)


def encode_frame_end():
    return MAGIC + _MSGTYPE.pack(MSG_FRAME_END)


class FrameAssembler:
    """Reassembles frames from the datagram sequence the HAL sends.

    Mirrors the vendor sample's two states: searching for a frame start, then filling until
    the announced size arrives. A control datagram that lands while filling means the frame
    lost data; the partial frame is dropped and a new start begins a fresh frame.
    """

    def __init__(self):
        self.header = None
        self._buf = None
        self.frames = 0
        self.dropped = 0
        self.malformed = 0
        self.ignored = 0

    @property
    def filling(self):
        return self._buf is not None

    def feed(self, datagram):
        """Consume one datagram; return a ``Frame`` when it completes one, else ``None``."""
        control = parse_control(datagram)
        if self._buf is None:
            if control is None:
                self.ignored += 1
                return None
            self._start(control)
            return None
        if control is not None:
            self.dropped += 1
            self._buf = None
            self._start(control)
            return None
        self._buf.extend(datagram)
        if len(self._buf) < self.header.size:
            return None
        data = bytes(self._buf[: self.header.size])
        self._buf = None
        self.frames += 1
        return Frame(self.header, data)

    def _start(self, control):
        msgtype, header = control
        if msgtype != MSG_FRAME_START:
            return
        if header is None:
            self.malformed += 1
            return
        self.header = header
        self._buf = bytearray()
