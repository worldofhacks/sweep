"""Pure-function tests for the RPLIDAR request builder and response parsers."""

import pytest

from adapters.ohmni.spike import rplidar_protocol as rp


def test_bare_requests_are_sync_plus_command():
    assert rp.stop_request() == b"\xa5\x25"
    assert rp.reset_request() == b"\xa5\x40"
    assert rp.scan_request() == b"\xa5\x20"
    assert rp.get_info_request() == b"\xa5\x50"
    assert rp.get_health_request() == b"\xa5\x52"


def test_set_motor_pwm_frame_carries_length_and_xor_checksum():
    frame = rp.set_motor_pwm_request(660)
    assert frame[:5] == bytes([0xA5, 0xF0, 0x02, 0x94, 0x02])
    expected = 0
    for byte in frame[:5]:
        expected ^= byte
    assert frame[5] == expected == 0xC1
    assert len(frame) == 6
    assert rp.set_motor_pwm_request(0) == bytes([0xA5, 0xF0, 0x02, 0x00, 0x00, 0x57])


@pytest.mark.parametrize("pwm", [-1, 1024])
def test_set_motor_pwm_rejects_out_of_range(pwm):
    with pytest.raises(ValueError):
        rp.set_motor_pwm_request(pwm)


def test_parse_descriptor_scan_info_health():
    scan = rp.parse_descriptor(bytes.fromhex("a55a0500004081"))
    assert scan == rp.Descriptor(5, rp.SEND_MODE_MULTI, rp.TYPE_SCAN)
    assert rp.parse_descriptor(bytes.fromhex("a55a1400000004")) == rp.Descriptor(20, 0, 4)
    assert rp.parse_descriptor(bytes.fromhex("a55a0300000006")) == rp.Descriptor(3, 0, 6)
    assert "0x81" in repr(scan)


def test_parse_descriptor_rejects_bad_sync_and_short_input():
    with pytest.raises(rp.ProtocolError):
        rp.parse_descriptor(bytes.fromhex("a5a50500004081"))
    with pytest.raises(rp.ProtocolError):
        rp.parse_descriptor(b"\xa5\x5a\x05")


def test_parse_info_and_health():
    serial = bytes(range(16))
    info = rp.parse_info(bytes([0x28, 0x1C, 0x01, 0x07]) + serial)
    assert info == {"model": 0x28, "firmware": "1.28", "hardware": 7, "serial": serial.hex()}
    assert rp.parse_health(b"\x00\x00\x00") == {
        "status": 0,
        "status_text": "good",
        "error_code": 0,
    }
    assert rp.parse_health(b"\x02\x34\x12") == {
        "status": 2,
        "status_text": "error",
        "error_code": 0x1234,
    }
    with pytest.raises(rp.ProtocolError):
        rp.parse_info(b"\x00" * 19)


def test_measurement_roundtrip_with_new_scan_flag():
    packet = rp.encode_measurement(True, 15, 0.0, 1234.5)
    assert packet[0] & 0b11 == 0b01
    point = rp.parse_measurement(packet)
    assert point.new_scan
    assert point.quality == 15
    assert point.angle_deg == 0.0
    assert point.distance_mm == 1234.5
    assert point.valid
    assert point.distance_m == 1.2345


def test_measurement_hand_built_bytes():
    # quality 10, not a new scan: b0 = 10 << 2 | 0b10; angle 90 deg = 5760 q6 = 0x1680, so
    # b1 = (0x00 << 1) | 1 and b2 = 0x2d; distance 1000 mm = 4000 q2 = 0x0fa0.
    point = rp.parse_measurement(bytes([0x2A, 0x01, 0x2D, 0xA0, 0x0F]))
    assert not point.new_scan
    assert point.quality == 10
    assert point.angle_deg == 90.0
    assert point.distance_mm == 1000.0


def test_zero_distance_is_an_invalid_return():
    point = rp.parse_measurement(rp.encode_measurement(False, 0, 12.5, 0))
    assert point.distance_mm == 0
    assert not point.valid


def test_parse_measurement_rejects_bad_flag_bits():
    both_start_bits = bytearray(rp.encode_measurement(False, 3, 10.0, 500))
    both_start_bits[0] |= 0b11
    with pytest.raises(rp.ProtocolError):
        rp.parse_measurement(bytes(both_start_bits))
    check_bit_clear = bytearray(rp.encode_measurement(False, 3, 10.0, 500))
    check_bit_clear[1] &= 0xFE
    with pytest.raises(rp.ProtocolError):
        rp.parse_measurement(bytes(check_bit_clear))
    with pytest.raises(rp.ProtocolError):
        rp.parse_measurement(b"\x01\x01")


def test_scan_parser_resyncs_and_collector_splits_revolutions():
    points = [
        (True, 10, 0.0, 1000),
        (False, 10, 90.0, 0),
        (False, 10, 180.0, 2000),
        (True, 10, 0.5, 1500),
        (False, 9, 90.0, 1600),
        (True, 8, 0.0, 1700),
    ]
    stream = b"\x00\x00" + b"".join(rp.encode_measurement(*point) for point in points)
    parser = rp.ScanParser()
    collector = rp.RevolutionCollector()
    revolutions = []
    for offset in range(0, len(stream), 3):
        revolutions.extend(collector.feed(parser.feed(stream[offset : offset + 3])))
    assert parser.resyncs == 2
    assert [len(revolution) for revolution in revolutions] == [3, 2]
    assert [point.angle_deg for point in revolutions[0]] == [0.0, 90.0, 180.0]
    assert rp.summarize_revolution(revolutions[0]) == {
        "points": 3,
        "valid": 2,
        "min_m": 1.0,
        "max_m": 2.0,
    }
    assert rp.revolution_record(1.5, revolutions[1]) == {
        "t": 1.5,
        "angles_deg": [0.5, 90.0],
        "ranges_m": [1.5, 1.6],
        "qualities": [10, 9],
    }


def test_collector_discards_points_before_the_first_scan_start():
    collector = rp.RevolutionCollector()
    tail = [rp.parse_measurement(rp.encode_measurement(False, 1, a, 100)) for a in (300, 350)]
    assert collector.feed(tail) == []
    first = rp.parse_measurement(rp.encode_measurement(True, 1, 0.0, 100))
    assert collector.feed([first]) == []
    second = rp.parse_measurement(rp.encode_measurement(True, 1, 0.0, 100))
    (revolution,) = collector.feed([second])
    assert revolution == [first]
    assert rp.summarize_revolution([]) == {"points": 0, "valid": 0, "min_m": None, "max_m": None}
