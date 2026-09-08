"""No robot/serial access: protocol, freshness, ownership, and recovery checks."""

from __future__ import annotations

import errno
import stat
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.ohmni import rplidar as module
from adapters.ohmni.rplidar import EMPTY_SCAN, Lidar, LidarError, _Revolution, _Stopped


def sample(angle, distance_mm=2000, *, start=False, quality=15):
    angle_q6 = round(angle * 64)
    distance_q2 = round(distance_mm * 4)
    return bytes([(quality << 2) | (1 if start else 2)]) + struct.pack(
        "<HH", (angle_q6 << 1) | 1, distance_q2
    )


def revolution(accumulator=None, *, now=100.0, step=5, quality=15):
    accumulator = accumulator or _Revolution()
    for angle in range(0, 360, step):
        assert (
            accumulator.feed(sample(angle, start=angle == 0, quality=quality), now + angle / 3600)
            is None
        )
    return accumulator, accumulator.feed(sample(0, start=True), now + 0.1)


def descriptor(size, mode, kind):
    return b"\xa5\x5a" + struct.pack("<I", size | mode << 30) + bytes([kind])


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.0)
    return Lidar(Mock())


def test_partial_first_revolution_is_discarded():
    accumulator = _Revolution()
    for angle in range(30, 360, 5):
        assert accumulator.feed(sample(angle), 100.0) is None
    accumulator, result = revolution(accumulator)
    bins, updated = result
    assert updated == 100.0
    assert len(bins) == 360
    assert sum(value > 0 for value in bins) == 72


def test_bin_keeps_closest_hit_and_floors_centimetres():
    accumulator = _Revolution()
    accumulator.feed(sample(0, 29, start=True), 100.0)
    accumulator.feed(sample(0.5, 5000), 100.0)
    for angle in range(5, 360, 5):
        accumulator.feed(sample(angle), 100.0)
    bins, _ = accumulator.feed(sample(0, start=True), 100.1)
    assert bins[0] == 2


def test_quality_zero_and_zero_distance_do_not_claim_free_space():
    accumulator = _Revolution()
    accumulator.feed(sample(0, start=True), 100.0)
    accumulator.feed(sample(1, 100, quality=0), 100.0)
    accumulator.feed(sample(2, 0), 100.0)
    for angle in range(5, 360, 5):
        accumulator.feed(sample(angle), 100.0)
    bins, _ = accumulator.feed(sample(0, start=True), 100.1)
    assert bins[1:3] == (0, 0)


@pytest.mark.parametrize(
    "packet",
    [b"\x00\x01\x00\x00\x00", b"\x03\x01\x00\x00\x00", b"\x3e\x00\x00\x00\x00", sample(400)],
)
def test_invalid_packet_is_rejected(packet):
    with pytest.raises(LidarError):
        _Revolution().feed(packet, 100.0)


@pytest.mark.parametrize("step,quality", [(20, 15), (5, 0)])
def test_insufficient_coverage_is_rejected(step, quality):
    with pytest.raises(LidarError, match="coverage"):
        revolution(step=step, quality=quality)


def test_three_quadrants_are_required_even_with_many_bins():
    accumulator = _Revolution()
    for angle in range(360):
        accumulator.feed(sample(angle, start=angle == 0, quality=15 if angle < 180 else 0), 100.0)
    with pytest.raises(LidarError, match="2 quadrants"):
        accumulator.feed(sample(0, start=True), 100.1)


def test_scan_without_full_angular_revolution_is_rejected():
    accumulator = _Revolution()
    for angle in range(0, 250, 5):
        accumulator.feed(sample(angle, start=angle == 0), 100.0)
    with pytest.raises(LidarError, match="coverage"):
        accumulator.feed(sample(250, start=True), 100.1)


def test_slow_revolution_is_rejected():
    accumulator = _Revolution()
    for angle in range(0, 360, 5):
        accumulator.feed(sample(angle, start=angle == 0), 100.0)
    with pytest.raises(LidarError, match="stale"):
        accumulator.feed(sample(0, start=True), 100.8)


def test_snapshot_is_immutable_and_stale_samples_are_cleared(driver, monkeypatch):
    bins = (100,) * 360
    driver._publish(bins, 100.0)
    assert driver.snapshot() == (bins, 100.0)
    assert driver.healthy
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.76)
    assert driver.snapshot() == (EMPTY_SCAN, 0.0)
    assert not driver.healthy
    assert "stale" in driver.fault_reason


def test_invalidating_clears_a_previously_healthy_snapshot(driver):
    driver._publish((100,) * 360, 100.0)
    driver._invalidate("USB disconnected")
    assert driver.snapshot() == (EMPTY_SCAN, 0.0)
    assert driver.fault_reason == "USB disconnected"


def test_stop_prevents_late_publication(driver):
    driver.stop()
    driver._publish((100,) * 360, 100.0)
    assert not driver.healthy


def test_discovery_walks_real_usb_ancestors_and_never_selects_ftdi(tmp_path, monkeypatch):
    serial = tmp_path / "sysfs"
    serial.mkdir()
    for name, vendor, product in [("ttyUSB0", "0403", "6015"), ("ttyUSB1", "10c4", "ea60")]:
        usb = tmp_path / name
        (usb / "interface" / "port" / name).mkdir(parents=True)
        (usb / "idVendor").write_text(vendor)
        (usb / "idProduct").write_text(product)
        (serial / name).symlink_to(usb / "interface" / "port" / name)
    monkeypatch.setattr(Lidar, "SYSFS_DIRECTORY", str(serial))
    real_stat = module.os.stat

    def fake_stat(path, *args, **kwargs):
        if str(path).startswith("/dev/ttyUSB"):
            return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=int(str(path)[-1]))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "stat", fake_stat)
    assert Lidar.discover() == "/dev/ttyUSB1"
    with pytest.raises(LidarError, match="not a present CP210x"):
        Lidar._verified_port("/dev/ttyUSB0")


def test_discovery_refuses_ambiguous_adapters(monkeypatch):
    monkeypatch.setattr(Lidar, "_ports", classmethod(lambda cls: ["/dev/ttyUSB0", "/dev/ttyUSB1"]))
    assert Lidar.discover() is None


def test_explicit_port_accepts_only_an_alias_of_verified_device(tmp_path, monkeypatch):
    port = tmp_path / "ttyUSB1"
    alias = tmp_path / "hub-port"
    alias.symlink_to(port)
    monkeypatch.setattr(Lidar, "_ports", classmethod(lambda cls: [str(port)]))
    assert Lidar._verified_port(str(alias)) == str(port)
    with pytest.raises(LidarError):
        Lidar._verified_port(str(tmp_path / "ttyUSB0"))


@pytest.mark.parametrize(
    "raw",
    [
        descriptor(6, 1, 0x81),
        descriptor(5, 0, 0x81),
        descriptor(5, 1, 0x82),
        b"\x00\x5a" + descriptor(5, 1, 0x81)[2:],
    ],
)
def test_wrong_descriptor_length_mode_type_or_magic_fails(driver, monkeypatch, raw):
    monkeypatch.setattr(driver, "_read_exact", lambda *_args: raw)
    with pytest.raises(LidarError, match="descriptor"):
        driver._descriptor(42, 5, 1, 0x81, 101.0)


def test_exact_read_handles_fragmented_usb_delivery(driver, monkeypatch):
    chunks = iter([b"\xa5", b"\x5a\x05\x00", b"\x00\x40\x81"])
    monkeypatch.setattr(driver, "_read", lambda *_args: next(chunks))
    driver._descriptor(42, 5, 1, 0x81, 101.0)


def test_a2_startup_validates_identity_health_before_pwm(driver, monkeypatch):
    writes = []
    monkeypatch.setattr(driver, "_write", lambda _fd, data, **_kw: writes.append(data))
    monkeypatch.setattr(driver, "_pause", lambda *_args: None)
    monkeypatch.setattr(module.termios, "tcflush", lambda *_args: None)
    monkeypatch.setattr(module.fcntl, "ioctl", lambda *_args: writes.append("DTR-clear"))
    monkeypatch.setattr(
        driver, "_query", Mock(side_effect=[bytes([0x28, 2, 1, 0]) + bytes(16), bytes(3)])
    )
    descriptor_mock = Mock()
    monkeypatch.setattr(driver, "_descriptor", descriptor_mock)
    driver._startup(42)
    assert driver._query.call_args_list[0].args == (42, 0x50, 20, 0x04)
    assert driver._query.call_args_list[1].args == (42, 0x52, 3, 0x06)
    assert writes == [b"\xa5\x25", "DTR-clear", b"\xa5\xf0\x02\x94\x02\xc1", b"\xa5\x20"]
    assert descriptor_mock.call_args.args[1:4] == (5, 1, 0x81)


@pytest.mark.parametrize(
    "responses",
    [
        [bytes([0x18]) + bytes(19)],
        [bytes([0x28]) + bytes(19), b"\x01\x00\x00"],
        [bytes([0x28]) + bytes(19), b"\x00\x01\x00"],
    ],
)
def test_wrong_model_or_unhealthy_sensor_never_receives_start_pwm(driver, monkeypatch, responses):
    monkeypatch.setattr(driver, "_write", Mock())
    monkeypatch.setattr(driver, "_pause", lambda *_args: None)
    monkeypatch.setattr(module.termios, "tcflush", lambda *_args: None)
    monkeypatch.setattr(driver, "_query", Mock(side_effect=responses))
    motor = Mock()
    monkeypatch.setattr(driver, "_motor", motor)
    with pytest.raises(LidarError):
        driver._startup(42)
    motor.assert_not_called()


def test_serial_read_timeout_is_bounded(driver, monkeypatch):
    clock = iter([100.0, 100.04, 100.08, 100.12])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(module.select, "select", lambda *_args: ([], [], []))
    with pytest.raises(LidarError, match="timed out"):
        driver._read(42, 5, 100.1)


def test_serial_disconnect_is_immediate(driver, monkeypatch):
    monkeypatch.setattr(module.select, "select", lambda *_args: ([42], [], []))
    monkeypatch.setattr(module.os, "read", lambda *_args: b"")
    with pytest.raises(LidarError, match="disconnected"):
        driver._read(42, 5, 101.0)


def test_write_handles_partial_writes_and_eagain(driver, monkeypatch):
    monkeypatch.setattr(module.select, "select", lambda *_args: ([], [42], []))
    write = Mock(side_effect=[BlockingIOError(errno.EAGAIN, "again"), 1, 2])
    monkeypatch.setattr(module.os, "write", write)
    driver._write(42, b"abc")
    assert [call.args[1] for call in write.call_args_list] == [b"abc", b"abc", b"bc"]


def test_open_configuration_failure_always_releases_fd(driver, monkeypatch):
    monkeypatch.setattr(driver, "_verified_port", lambda port: port)
    monkeypatch.setattr(module.os, "open", lambda *_args: 42)
    monkeypatch.setattr(module.os, "fstat", lambda *_args: SimpleNamespace(st_rdev=1))
    monkeypatch.setattr(module.os, "stat", lambda *_args: SimpleNamespace(st_rdev=1))
    monkeypatch.setattr(module.fcntl, "flock", lambda *_args: None)
    ioctl = Mock()
    monkeypatch.setattr(module.fcntl, "ioctl", ioctl)
    monkeypatch.setattr(module.termios, "tcgetattr", Mock(side_effect=OSError("config failed")))
    close = Mock()
    monkeypatch.setattr(module.os, "close", close)
    with pytest.raises(OSError, match="config failed"):
        driver._open("/dev/ttyUSB1")
    close.assert_called_once_with(42)
    assert ioctl.call_args.args[1] == getattr(module.termios, "TIOCNXCL", 0x540D)


def test_duplicate_owner_is_rejected_before_vendor_commands(driver, monkeypatch):
    monkeypatch.setattr(driver, "_verified_port", lambda port: port)
    monkeypatch.setattr(module.os, "open", lambda *_args: 42)
    monkeypatch.setattr(module.fcntl, "flock", Mock(side_effect=BlockingIOError("already owned")))
    close = Mock()
    monkeypatch.setattr(module.os, "close", close)
    with pytest.raises(BlockingIOError):
        driver._open("/dev/ttyUSB1")
    close.assert_called_once_with(42)
    driver.shell.command.assert_not_called()


def test_cleanup_attempts_pwm_dtr_and_close_even_when_scan_stop_fails(driver, monkeypatch):
    monkeypatch.setattr(driver, "_write", Mock(side_effect=OSError("disconnected")))
    motor = Mock(side_effect=OSError("disconnected"))
    monkeypatch.setattr(driver, "_motor", motor)
    ioctl = Mock(side_effect=OSError("disconnected"))
    monkeypatch.setattr(module.fcntl, "ioctl", ioctl)
    close = Mock()
    monkeypatch.setattr(module.os, "close", close)
    driver._close(42)
    motor.assert_called_once_with(42, 0, cleanup=True)
    assert ioctl.call_count == 2
    close.assert_called_once_with(42)


def test_absent_at_startup_is_rediscovered_and_can_recover(driver, monkeypatch, tmp_path):
    monkeypatch.setattr(driver, "OWNER_LOCK_PATH", str(tmp_path / "owner.lock"))
    discover = Mock(side_effect=[None, "/dev/ttyUSB1"])
    monkeypatch.setattr(driver, "discover", discover)
    monkeypatch.setattr(driver, "_open", Mock(return_value=42))
    monkeypatch.setattr(driver, "_startup", Mock())
    monkeypatch.setattr(driver, "_close", Mock())
    real_stop = driver._stop
    monkeypatch.setattr(real_stop, "wait", lambda _seconds: False)

    def scanned(_fd):
        assert driver.port == "/dev/ttyUSB1"
        real_stop.set()
        raise _Stopped()

    monkeypatch.setattr(driver, "_scan", scanned)
    driver._run()
    assert discover.call_count == 2
    driver._open.assert_called_once_with("/dev/ttyUSB1")
    driver._close.assert_called_once_with(42)


def test_disconnect_clears_scan_before_cleanup_and_retry(driver, monkeypatch, tmp_path):
    monkeypatch.setattr(driver, "OWNER_LOCK_PATH", str(tmp_path / "owner.lock"))
    monkeypatch.setattr(driver, "discover", lambda: "/dev/ttyUSB1")
    monkeypatch.setattr(driver, "_open", lambda _port: 42)
    monkeypatch.setattr(driver, "_startup", lambda _fd: None)

    def scan(_fd):
        driver._publish((100,) * 360, 100.0)
        raise OSError("USB disconnected")

    def close(_fd):
        assert driver.snapshot() == (EMPTY_SCAN, 0.0)
        assert "disconnected" in driver.fault_reason
        driver._stop.set()

    monkeypatch.setattr(driver, "_scan", scan)
    monkeypatch.setattr(driver, "_close", close)
    driver._run()


def test_scan_backlog_is_rejected_without_using_old_bytes(driver, monkeypatch):
    monkeypatch.setattr(module.fcntl, "ioctl", lambda *_args: struct.pack("I", 5000))
    with pytest.raises(LidarError, match="backlog"):
        driver._scan(42)


def test_stop_joins_worker_and_interrupts_retry_sleep(monkeypatch):
    driver = Lidar(Mock())
    monkeypatch.setattr(driver, "discover", lambda: None)
    driver.start()
    driver.stop()
    assert not driver._thread.is_alive()
    assert not driver.healthy


def test_owner_lock_prevents_duplicate_vendor_handoff(driver, monkeypatch, tmp_path):
    monkeypatch.setattr(Lidar, "OWNER_LOCK_PATH", str(tmp_path / "owner.lock"))
    owner = driver._claim()
    duplicate = Lidar(Mock())
    try:
        with pytest.raises(BlockingIOError):
            duplicate._claim()
        duplicate.shell.command.assert_not_called()
    finally:
        module.os.close(owner)
    replacement = duplicate._claim()
    module.os.close(replacement)


def test_vendor_handoff_precedes_serial_configuration(driver, monkeypatch, tmp_path):
    monkeypatch.setattr(driver, "OWNER_LOCK_PATH", str(tmp_path / "owner.lock"))
    monkeypatch.setattr(driver, "discover", lambda: "/dev/ttyUSB1")
    events = []
    monkeypatch.setattr(driver, "_release_vendor", lambda: events.append("release"))
    monkeypatch.setattr(driver, "_open", lambda _port: events.append("open") or 42)
    monkeypatch.setattr(driver, "_startup", lambda _fd: events.append("startup"))
    monkeypatch.setattr(driver, "_close", lambda _fd: events.append("close"))

    def scan(_fd):
        driver._stop.set()
        raise _Stopped()

    monkeypatch.setattr(driver, "_scan", scan)
    driver._run()
    assert events == ["release", "open", "startup", "close"]


def test_explicit_android_character_alias_matches_verified_device(driver, monkeypatch):
    monkeypatch.setattr(Lidar, "_ports", classmethod(lambda cls: ["/dev/ttyUSB1"]))
    monkeypatch.setattr(
        module.os, "stat", lambda *_args: SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=123)
    )
    assert Lidar._verified_port("/dev/usb/tty1-2.1") == "/dev/ttyUSB1"


def test_open_uses_115200_raw_8n1_and_clears_dtr(driver, monkeypatch):
    monkeypatch.setattr(driver, "_verified_port", lambda port: port)
    monkeypatch.setattr(module.os, "open", lambda *_args: 42)
    monkeypatch.setattr(module.os, "fstat", lambda *_args: SimpleNamespace(st_rdev=1))
    monkeypatch.setattr(module.os, "stat", lambda *_args: SimpleNamespace(st_rdev=1))
    monkeypatch.setattr(module.fcntl, "flock", lambda *_args: None)
    ioctl = Mock()
    monkeypatch.setattr(module.fcntl, "ioctl", ioctl)
    monkeypatch.setattr(
        module.termios, "tcgetattr", lambda *_args: [999, 999, 999, 999, 9600, 9600, [0] * 32]
    )
    setattrs = Mock()
    monkeypatch.setattr(module.termios, "tcsetattr", setattrs)
    monkeypatch.setattr(module.termios, "tcflush", lambda *_args: None)
    assert driver._open("/dev/ttyUSB1") == 42
    attrs = setattrs.call_args.args[2]
    assert attrs[:4] == [0, 0, module.termios.CS8 | module.termios.CREAD | module.termios.CLOCAL, 0]
    assert attrs[4:6] == [module.termios.B115200, module.termios.B115200]
    assert ioctl.call_args.args[1:] == (
        getattr(module.termios, "TIOCMBIC", 0x5417),
        struct.pack("I", module.termios.TIOCM_DTR),
    )


def test_stream_parser_handles_chunk_boundaries_and_publishes_complete_scan(driver, monkeypatch):
    stream = b"".join(sample(angle, start=angle == 0) for angle in range(0, 360, 5)) + sample(
        0, start=True
    )
    chunks = iter([stream[:3], stream[3:151], stream[151:]])
    monkeypatch.setattr(module.fcntl, "ioctl", lambda *_args: struct.pack("I", 0))

    def read(*_args):
        try:
            return next(chunks)
        except StopIteration:
            assert driver.healthy
            bins, updated = driver.snapshot()
            assert updated == 100.0 and sum(value > 0 for value in bins) == 72
            raise _Stopped() from None

    monkeypatch.setattr(driver, "_read", read)
    with pytest.raises(_Stopped):
        driver._scan(42)


@pytest.mark.parametrize(
    "previous,current", [(13.453125, 8.421875), (12.6875, 7.640625), (35.640625, 30.578125)]
)
def test_measured_a2m8_backwards_angle_correction_preserves_full_revolution(previous, current):
    accumulator = _Revolution()
    accumulator.feed(sample(0, start=True), 100.0)
    for angle in range(1, int(previous) + 1):
        accumulator.feed(sample(angle), 100.0)
    accumulator.feed(sample(previous), 100.0)
    accumulator.feed(sample(current), 100.0)
    assert accumulator.sweep == pytest.approx(current)
    for angle in range(int(previous) + 1, 360):
        accumulator.feed(sample(angle), 100.0)
    bins, _updated = accumulator.feed(sample(0, start=True), 100.1)
    assert sum(value > 0 for value in bins) >= 350


def test_back_and_forth_angle_corrections_cannot_fabricate_a_complete_revolution():
    accumulator = _Revolution()
    accumulator.feed(sample(0, start=True), 100.0)
    for angle in range(1, 241):
        accumulator.feed(sample(angle), 100.0)
    for _ in range(100):
        accumulator.feed(sample(235), 100.0)
        accumulator.feed(sample(240), 100.0)
    assert accumulator.sweep == 240
    with pytest.raises(LidarError, match="coverage"):
        accumulator.feed(sample(245, start=True), 100.1)


def test_cross_zero_backwards_correction_cancels_instead_of_adding_a_revolution():
    accumulator = _Revolution()
    accumulator.feed(sample(1, start=True), 100.0)
    accumulator.feed(sample(359), 100.0)
    assert accumulator.sweep == -2
    accumulator.feed(sample(1), 100.0)
    assert accumulator.sweep == 0
    for angle in range(2, 360):
        accumulator.feed(sample(angle), 100.0)
    bins, _updated = accumulator.feed(sample(1, start=True), 100.1)
    assert sum(value > 0 for value in bins) == 359
