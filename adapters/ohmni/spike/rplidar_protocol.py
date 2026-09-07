"""Slamtec RPLIDAR A2 serial protocol: request frames and response parsing.

Pure functions and small classes with no I/O, so the same code serves the S0 spike probe
and a later Ohmni node. Runs on the container's Python 3.6.

Wire format over 115200 8N1:

* request: ``A5 <cmd>``, or ``A5 <cmd> <len> <payload...> <checksum>`` where the checksum
  is the XOR of every preceding byte;
* response descriptor: ``A5 5A``, a 32-bit little-endian word whose low 30 bits are the
  data length and high 2 bits the send mode, then one data-type byte;
* standard scan measurement: 5 bytes, laid out as ``parse_measurement`` describes.
"""

import struct

SYNC = 0xA5
SYNC2 = 0x5A

CMD_STOP = 0x25
CMD_RESET = 0x40
CMD_SCAN = 0x20
CMD_GET_INFO = 0x50
CMD_GET_HEALTH = 0x52
CMD_SET_MOTOR_PWM = 0xF0

DESCRIPTOR_LEN = 7
MEASUREMENT_LEN = 5
INFO_LEN = 20
HEALTH_LEN = 3

TYPE_INFO = 0x04
TYPE_HEALTH = 0x06
TYPE_SCAN = 0x81

SEND_MODE_SINGLE = 0
SEND_MODE_MULTI = 1

HEALTH_STATUS = {0: "good", 1: "warning", 2: "error"}

DEFAULT_MOTOR_PWM = 660
MAX_MOTOR_PWM = 1023


class ProtocolError(ValueError):
    """Bytes that do not form a valid RPLIDAR response."""


def checksum(data):
    """XOR of every byte; the request checksum covers sync, command, length, and payload."""
    value = 0
    for byte in data:
        value ^= byte
    return value


def request(cmd, payload=b""):
    """Build a request frame; a payload adds the length byte and the trailing checksum."""
    if not payload:
        return bytes([SYNC, cmd])
    if len(payload) > 255:
        raise ValueError("payload longer than 255 bytes")
    head = bytes([SYNC, cmd, len(payload)]) + bytes(payload)
    return head + bytes([checksum(head)])


def stop_request():
    return request(CMD_STOP)


def reset_request():
    return request(CMD_RESET)


def scan_request():
    return request(CMD_SCAN)


def get_info_request():
    return request(CMD_GET_INFO)


def get_health_request():
    return request(CMD_GET_HEALTH)


def set_motor_pwm_request(pwm):
    """``A5 F0 02 <pwm lo> <pwm hi> <checksum>``; 0 stops the motor, 660 is the default."""
    if not 0 <= pwm <= MAX_MOTOR_PWM:
        raise ValueError(f"motor pwm {pwm} outside 0..{MAX_MOTOR_PWM}")
    return request(CMD_SET_MOTOR_PWM, struct.pack("<H", pwm))


class Descriptor:
    """A response descriptor: how many bytes follow, once or repeatedly, and their type."""

    __slots__ = ("data_length", "send_mode", "data_type")

    def __init__(self, data_length, send_mode, data_type):
        self.data_length = data_length
        self.send_mode = send_mode
        self.data_type = data_type

    def __eq__(self, other):
        return isinstance(other, Descriptor) and (
            self.data_length,
            self.send_mode,
            self.data_type,
        ) == (other.data_length, other.send_mode, other.data_type)

    def __repr__(self):
        return (
            f"Descriptor(data_length={self.data_length}, send_mode={self.send_mode}, "
            f"data_type=0x{self.data_type:02x})"
        )


def parse_descriptor(buf):
    """Parse the 7-byte response descriptor at the start of ``buf``."""
    if len(buf) < DESCRIPTOR_LEN:
        raise ProtocolError(f"descriptor needs {DESCRIPTOR_LEN} bytes, got {len(buf)}")
    if buf[0] != SYNC or buf[1] != SYNC2:
        raise ProtocolError(f"bad descriptor sync bytes {bytes(buf[:2]).hex()}")
    word = struct.unpack("<I", bytes(buf[2:6]))[0]
    return Descriptor(word & 0x3FFFFFFF, word >> 30, buf[6])


def parse_info(payload):
    """GET_INFO payload: model, firmware minor, firmware major, hardware, 16-byte serial."""
    if len(payload) < INFO_LEN:
        raise ProtocolError(f"info needs {INFO_LEN} bytes, got {len(payload)}")
    return {
        "model": payload[0],
        "firmware": f"{payload[2]}.{payload[1]}",
        "hardware": payload[3],
        "serial": bytes(payload[4:20]).hex(),
    }


def parse_health(payload):
    """GET_HEALTH payload: status byte (0 good, 1 warning, 2 error) and a LE16 error code."""
    if len(payload) < HEALTH_LEN:
        raise ProtocolError(f"health needs {HEALTH_LEN} bytes, got {len(payload)}")
    status = payload[0]
    code = struct.unpack("<H", bytes(payload[1:3]))[0]
    return {
        "status": status,
        "status_text": HEALTH_STATUS.get(status, "unknown"),
        "error_code": code,
    }


class Measurement:
    """One standard-scan point. ``distance_mm`` of 0 means no valid return at that angle."""

    __slots__ = ("new_scan", "quality", "angle_deg", "distance_mm")

    def __init__(self, new_scan, quality, angle_deg, distance_mm):
        self.new_scan = new_scan
        self.quality = quality
        self.angle_deg = angle_deg
        self.distance_mm = distance_mm

    @property
    def valid(self):
        return self.distance_mm > 0

    @property
    def distance_m(self):
        return self.distance_mm / 1000.0

    def __repr__(self):
        return (
            f"Measurement(new_scan={self.new_scan}, quality={self.quality}, "
            f"angle_deg={self.angle_deg:.2f}, distance_mm={self.distance_mm:.2f})"
        )


def parse_measurement(pkt):
    """Parse one 5-byte standard scan packet.

    ``b0`` = quality << 2 | (not S) << 1 | S, where S set marks the first point of a new
    revolution and the two flag bits must disagree; ``b1`` = (angle_q6 low 7 bits) << 1 | 1,
    with the low check bit always set; ``b2`` = angle_q6 high 8 bits; ``b3 b4`` = distance_q2
    little-endian. angle_deg = angle_q6 / 64, distance_mm = distance_q2 / 4.
    """
    if len(pkt) < MEASUREMENT_LEN:
        raise ProtocolError(f"measurement needs {MEASUREMENT_LEN} bytes, got {len(pkt)}")
    b0, b1, b2, b3, b4 = pkt[0], pkt[1], pkt[2], pkt[3], pkt[4]
    start = b0 & 1
    not_start = (b0 >> 1) & 1
    if start == not_start:
        raise ProtocolError("start flag bits agree; not a packet boundary")
    if not b1 & 1:
        raise ProtocolError("angle check bit clear; not a packet boundary")
    angle_q6 = (b1 >> 1) | (b2 << 7)
    distance_q2 = b3 | (b4 << 8)
    return Measurement(bool(start), b0 >> 2, angle_q6 / 64.0, distance_q2 / 4.0)


def encode_measurement(new_scan, quality, angle_deg, distance_mm):
    """Inverse of ``parse_measurement`` for tests and fakes."""
    angle_q6 = int(round(angle_deg * 64)) & 0x7FFF
    distance_q2 = int(round(distance_mm * 4)) & 0xFFFF
    start = 1 if new_scan else 0
    b0 = ((quality & 0x3F) << 2) | ((1 - start) << 1) | start
    b1 = ((angle_q6 & 0x7F) << 1) | 1
    b2 = angle_q6 >> 7
    return bytes([b0, b1, b2, distance_q2 & 0xFF, distance_q2 >> 8])


class ScanParser:
    """Turns the scan byte stream into measurements, shifting one byte to resync on garbage."""

    def __init__(self):
        self._buf = bytearray()
        self.resyncs = 0

    def feed(self, data):
        """Append bytes and return every complete, well-formed measurement now available."""
        self._buf.extend(data)
        out = []
        while len(self._buf) >= MEASUREMENT_LEN:
            try:
                point = parse_measurement(self._buf[:MEASUREMENT_LEN])
            except ProtocolError:
                del self._buf[0]
                self.resyncs += 1
                continue
            del self._buf[:MEASUREMENT_LEN]
            out.append(point)
        return out


class RevolutionCollector:
    """Groups measurements into revolutions on the new-scan flag.

    Points before the first flagged point belong to a revolution whose start was missed and
    are discarded, so every returned revolution starts at its own first point.
    """

    def __init__(self):
        self._current = []
        self._started = False

    def feed(self, measurements):
        """Return the revolutions completed by these measurements, in order."""
        done = []
        for point in measurements:
            if point.new_scan:
                if self._started and self._current:
                    done.append(self._current)
                self._current = [point]
                self._started = True
            elif self._started:
                self._current.append(point)
        return done


def summarize_revolution(points):
    """Point count, valid count, and the nearest and farthest valid ranges in metres."""
    valid = [point.distance_mm for point in points if point.valid]
    return {
        "points": len(points),
        "valid": len(valid),
        "min_m": round(min(valid) / 1000.0, 3) if valid else None,
        "max_m": round(max(valid) / 1000.0, 3) if valid else None,
    }


def revolution_record(t, points):
    """One JSONL record per revolution; invalid returns keep a range of 0.0."""
    return {
        "t": t,
        "angles_deg": [round(point.angle_deg, 2) for point in points],
        "ranges_m": [round(point.distance_mm / 1000.0, 3) for point in points],
        "qualities": [point.quality for point in points],
    }
