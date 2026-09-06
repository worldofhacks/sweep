"""Safety-boundary tests for the Ohmni hardware spike executables."""

import pytest

from adapters.ohmni.spike import drive_probe, lidar_probe, ros_odom_log
from adapters.ohmni.spike import rplidar_protocol as rp


@pytest.mark.parametrize(
    "argv",
    [
        ["--speed", "0"],
        ["--speed", "21"],
        ["--side", "0"],
        ["--side", "5.1"],
        ["--legs", "0"],
        ["--legs", "5"],
        ["--settle", "0"],
        ["--settle", "11"],
        ["--leg-wait", "0"],
        ["--leg-wait", "61"],
        ["--turn-wait", "0"],
        ["--turn-wait", "31"],
        ["--poll-hz", "0"],
        ["--poll-hz", "21"],
        ["--timeout", "0"],
        ["--timeout", "6"],
        ["--side", "nan"],
        ["--leg-wait", "40", "--turn-wait", "10"],
    ],
)
def test_drive_probe_rejects_unbounded_plans(argv):
    args = drive_probe.build_parser().parse_args(argv)
    with pytest.raises(ValueError):
        drive_probe.validate_args(args)


def test_drive_probe_accepts_documented_five_metre_run():
    args = drive_probe.build_parser().parse_args(["--side", "5", "--leg-wait", "30"])
    drive_probe.validate_args(args)


def test_drive_log_refuses_an_existing_evidence_file(tmp_path):
    path = tmp_path / "drive.jsonl"
    path.write_text("prior evidence\n")
    with pytest.raises(FileExistsError):
        drive_probe.DriveLog(str(path))
    assert path.read_text() == "prior evidence\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["--side", "0"],
        ["--side", "5.1"],
        ["--speed", "0"],
        ["--speed", "0.26"],
        ["--turn-rate", "0"],
        ["--turn-rate", "0.76"],
        ["--stop", "0"],
        ["--stop", "11"],
        ["--cmd-rate", "0"],
        ["--cmd-rate", "51"],
        ["--rate", "0"],
        ["--rate", "51"],
        ["--seconds", "-1"],
        ["--seconds", "301"],
        ["--side", "nan"],
        ["--side", "5", "--speed", "0.05"],
        ["--turn-rate", "0.05"],
    ],
)
def test_ros_square_rejects_unbounded_motion(argv):
    args = ros_odom_log.build_parser().parse_args(["--drive-square"] + argv)
    with pytest.raises(ValueError):
        ros_odom_log.validate_args(args)


def test_ros_square_accepts_documented_five_metre_run():
    args = ros_odom_log.build_parser().parse_args(["--drive-square", "--side", "5"])
    ros_odom_log.validate_args(args)


@pytest.mark.parametrize("seconds", ["0", "-1", "301", "nan", "inf"])
def test_plain_ros_log_requires_a_finite_bounded_duration(seconds):
    args = ros_odom_log.build_parser().parse_args(["--seconds", seconds])
    with pytest.raises(ValueError):
        ros_odom_log.validate_args(args)


def test_ros_log_refuses_an_existing_evidence_file(tmp_path):
    path = tmp_path / "odom.jsonl"
    path.write_text("prior evidence\n")
    with pytest.raises(FileExistsError):
        ros_odom_log.TopicLogger(str(path))
    assert path.read_text() == "prior evidence\n"


def test_current_odom_requires_a_recent_monotonic_receipt(tmp_path):
    logger = ros_odom_log.TopicLogger(str(tmp_path / "odom.jsonl"))
    try:
        logger.latest["odom"] = {"x": 1.0}
        logger.latest_at["odom"] = 100.0
        assert logger.current_odom(1.0, now=100.5) == {"x": 1.0}
        assert logger.current_odom(1.0, now=101.1) is None
        assert logger.current_odom(1.0, now=99.9) is None
    finally:
        logger.close()


def test_ros_drive_refuses_to_create_publisher_without_current_odom(monkeypatch, tmp_path):
    class FakeTimer:
        def shutdown(self):
            pass

    class FakeRos:
        AnyMsg = object
        publisher_created = False

        @staticmethod
        def init_node(*args, **kwargs):
            pass

        @staticmethod
        def Subscriber(*args, **kwargs):
            pass

        @staticmethod
        def Duration(value):
            return value

        @staticmethod
        def Timer(*args, **kwargs):
            return FakeTimer()

        @staticmethod
        def is_shutdown():
            return False

        @classmethod
        def Publisher(cls, *args, **kwargs):
            cls.publisher_created = True
            raise AssertionError("motion publisher must not be created without odometry")

    class EmptyLogger:
        def __init__(self, path):
            pass

        def current_odom(self, max_age_s):
            return None

        def on_odom(self, message):
            pass

        def generic_callback(self, key):
            return lambda message: None

        def snapshot(self, key):
            return None

        def write_tick(self):
            pass

        def close(self):
            pass

        def summary(self):
            return "empty"

    monkeypatch.setattr(ros_odom_log, "rospy", FakeRos)
    monkeypatch.setattr(ros_odom_log, "Odometry", object, raising=False)
    monkeypatch.setattr(ros_odom_log, "TopicLogger", EmptyLogger)
    monkeypatch.setattr(ros_odom_log.time, "sleep", lambda seconds: None)
    code = ros_odom_log.main(["--drive-square", "--out", str(tmp_path / "unused.jsonl")])
    assert code == 2
    assert not FakeRos.publisher_created


def test_ros_drive_stops_when_odometry_freshness_is_lost(monkeypatch):
    class Vector:
        x = 0.0
        z = 0.0

    class FakeTwist:
        def __init__(self):
            self.linear = Vector()
            self.angular = Vector()

    class FakeRate:
        def sleep(self):
            pass

    class FakeRos:
        @staticmethod
        def Rate(hz):
            return FakeRate()

        @staticmethod
        def is_shutdown():
            return False

    published = []

    class FakePublisher:
        def publish(self, twist):
            published.append((twist.linear.x, twist.angular.z))

    monkeypatch.setattr(ros_odom_log, "rospy", FakeRos)
    monkeypatch.setattr(ros_odom_log, "Twist", FakeTwist, raising=False)
    monkeypatch.setattr(ros_odom_log.time, "sleep", lambda seconds: None)
    with pytest.raises(ros_odom_log.MotionSafetyError):
        ros_odom_log.drive_square(
            FakePublisher(),
            side_m=1.0,
            speed=0.2,
            turn_rate=0.5,
            stop_s=1.0,
            rate_hz=10.0,
            odom_is_current=lambda: False,
        )
    assert published == [(0.0, 0.0)] * 5


@pytest.mark.parametrize(
    "health",
    [
        {"status": 1, "status_text": "warning", "error_code": 7},
        {"status": 2, "status_text": "error", "error_code": 8},
        {"status": 99, "status_text": "unknown", "error_code": 9},
    ],
)
def test_lidar_rejects_every_non_good_health(health):
    with pytest.raises(lidar_probe.ProbeError, match="refusing to start motor or scan"):
        lidar_probe.require_good_health(health)


@pytest.mark.parametrize(
    "argv",
    [
        ["--seconds", "0"],
        ["--seconds", "-1"],
        ["--seconds", "301"],
        ["--seconds", "nan"],
        ["--seconds", "inf"],
        ["--timeout", "0"],
        ["--timeout", "6"],
        ["--pwm", "0"],
        ["--pwm", "1024"],
    ],
)
def test_lidar_rejects_unbounded_or_invalid_runs(argv):
    args = lidar_probe.build_parser().parse_args(argv)
    with pytest.raises(ValueError):
        lidar_probe.validate_args(args)


def test_lidar_config_read_is_bounded(tmp_path):
    path = tmp_path / "telebot_config.json"
    path.write_bytes(b" " * (lidar_probe.MAX_CONFIG_BYTES + 1))
    assert lidar_probe.configured_port(str(path)) is None


def test_lidar_run_validates_before_opening_evidence_or_hardware():
    args = lidar_probe.build_parser().parse_args(["--seconds", "0"])
    with pytest.raises(ValueError):
        lidar_probe.run(args)


def test_lidar_drain_retains_only_a_bounded_prefix(monkeypatch):
    port = lidar_probe.SerialPort("/dev/fake")
    now = [0.0]

    def monotonic():
        now[0] += 0.1
        return now[0]

    monkeypatch.setattr(lidar_probe, "MAX_DRAIN_BYTES", 10)
    monkeypatch.setattr(lidar_probe.time, "monotonic", monotonic)
    monkeypatch.setattr(port, "read", lambda max_bytes, timeout: b"x" * max_bytes)
    assert port.drain(0.5) == b"x" * 10


def test_lidar_single_response_rejects_descriptor_length_before_read(monkeypatch):
    class Port:
        read_count = None

        def write(self, data):
            pass

        def read_exact(self, count, timeout):
            self.read_count = count
            return b""

    port = Port()
    descriptor = rp.Descriptor(1_000_000, rp.SEND_MODE_SINGLE, rp.TYPE_INFO)
    monkeypatch.setattr(lidar_probe, "read_descriptor", lambda *args, **kwargs: descriptor)
    with pytest.raises(lidar_probe.ProbeError, match="unexpected payload length"):
        lidar_probe.single_response(port, rp.get_info_request(), rp.TYPE_INFO)
    assert port.read_count is None


def test_lidar_recorder_is_exclusive_and_byte_bounded(monkeypatch, tmp_path):
    path = tmp_path / "scans.jsonl"
    path.write_text("prior evidence\n")
    with pytest.raises(FileExistsError):
        lidar_probe.ScanRecorder(str(path))
    assert path.read_text() == "prior evidence\n"

    bounded = tmp_path / "bounded.jsonl"
    monkeypatch.setattr(lidar_probe, "MAX_RECORD_BYTES", 16)
    recorder = lidar_probe.ScanRecorder(str(bounded))
    try:
        with pytest.raises(lidar_probe.ProbeError, match="recording exceeds"):
            recorder.write({"payload": "too large"})
    finally:
        recorder.close()
    assert bounded.read_text() == ""


def test_lidar_non_good_health_never_starts_motor_or_scan(monkeypatch):
    class FakePort:
        def __init__(self):
            self.writes = []

        def open(self):
            return self

        def close(self):
            pass

        def set_dtr(self, asserted):
            pass

        def write(self, data):
            self.writes.append(data)

        def drain(self, seconds):
            return b""

    port = FakePort()

    def response(unused_port, request, expected_type, timeout=1.0):
        if expected_type == rp.TYPE_INFO:
            return bytes([0x28, 0x1C, 0x01, 0x07]) + bytes(range(16))
        assert expected_type == rp.TYPE_HEALTH
        return bytes([1, 7, 0])

    monkeypatch.setattr(lidar_probe, "SerialPort", lambda path: port)
    monkeypatch.setattr(lidar_probe, "single_response", response)
    monkeypatch.setattr(lidar_probe.time, "sleep", lambda seconds: None)
    args = lidar_probe.build_parser().parse_args(["--port", "/dev/fake", "--no-release"])
    with pytest.raises(lidar_probe.ProbeError):
        lidar_probe.run(args)
    assert rp.set_motor_pwm_request(rp.DEFAULT_MOTOR_PWM) not in port.writes
    assert rp.scan_request() not in port.writes
    assert rp.set_motor_pwm_request(0) in port.writes
