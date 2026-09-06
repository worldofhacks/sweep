#!/usr/bin/env python
# ruff: noqa: UP004, UP008, UP010, UP031, UP032, UP036
"""Log Ohmni wheel odometry and core status from ROS; optionally drive a square.

Runs on Python 2 (the ohmni_ros_tbcontrol container, ROS Melodic) and on Python 3. Inside
the container, after ``source /opt/ros/melodic/setup.bash``:

    python ros_odom_log.py --out odom.jsonl --seconds 60
    python ros_odom_log.py --out square.jsonl --drive-square

From a laptop with a ROS install on the same LAN it needs ``ROS_MASTER_URI=http://<bot_ip>:11311``
and ``ROS_IP=<laptop_ip>``.

Ten times a second it writes one JSON line with the latest ``/tb_control/wheel_odom`` sample
(x, y, yaw from the quaternion, vx, wz) and the latest ``/tb_control/tbcore_status`` and
``/tb_control/wheel_encoder`` messages, whose custom types are resolved at run time so their
field names need not be known here. ``--drive-square`` publishes ``geometry_msgs/Twist`` on
``/tb_cmd_vel``: four legs at the requested speed with a stop between legs and a 90-degree
turn with positive angular z (a left turn). A zero Twist is always published on exit, and
the closure error between the pose at the start and the end of the square is printed.
"""

from __future__ import print_function

import argparse
import json
import math
import os
import sys
import threading
import time

try:
    import roslib.message
    import rospy
    from geometry_msgs.msg import Twist
    from nav_msgs.msg import Odometry
except ImportError as exc:  # reported by main(); the module stays importable for --help
    rospy = None
    IMPORT_ERROR = exc

ODOM_TOPIC = "/tb_control/wheel_odom"
STATUS_TOPIC = "/tb_control/tbcore_status"
ENCODER_TOPIC = "/tb_control/wheel_encoder"
CMD_VEL_TOPIC = "/tb_cmd_vel"
MAX_SIDE_M = 5.0
MAX_SPEED_MPS = 0.25
MAX_TURN_RATE_RAD_S = 0.75
MAX_STOP_S = 10.0
MAX_CMD_RATE_HZ = 50.0
MAX_LOG_RATE_HZ = 50.0
MAX_LOG_SECONDS = 300.0
MAX_LEG_MOTION_S = 60.0
MAX_TURN_MOTION_S = 15.0
MAX_SQUARE_MOTION_S = 180.0
ODOM_WAIT_S = 3.0
ODOM_MAX_AGE_S = 1.0
_monotonic = getattr(time, "monotonic", time.time)


class MotionSafetyError(Exception):
    """A drive was refused or aborted because its safety precondition was lost."""


def _finite(value):
    return not (value != value or value in (float("inf"), float("-inf")))


def validate_args(args):
    """Refuse a ROS drive outside the bounded hardware-spike envelope."""
    numeric = {
        "side": args.side,
        "speed": args.speed,
        "turn rate": args.turn_rate,
        "stop": args.stop,
        "command rate": args.cmd_rate,
        "log rate": args.rate,
        "log time": args.seconds,
    }
    for name, value in numeric.items():
        if not _finite(value):
            raise ValueError("%s must be finite" % name)
    if not 0 < args.side <= MAX_SIDE_M:
        raise ValueError("side must be within 0..%g m" % MAX_SIDE_M)
    if not 0 < args.speed <= MAX_SPEED_MPS:
        raise ValueError("speed must be within 0..%g m/s" % MAX_SPEED_MPS)
    if not 0 < args.turn_rate <= MAX_TURN_RATE_RAD_S:
        raise ValueError("turn rate must be within 0..%g rad/s" % MAX_TURN_RATE_RAD_S)
    if not 0 < args.stop <= MAX_STOP_S:
        raise ValueError("stop must be within 0..%g s" % MAX_STOP_S)
    if not 0 < args.cmd_rate <= MAX_CMD_RATE_HZ:
        raise ValueError("command rate must be within 0..%g Hz" % MAX_CMD_RATE_HZ)
    if not 0 < args.rate <= MAX_LOG_RATE_HZ:
        raise ValueError("log rate must be within 0..%g Hz" % MAX_LOG_RATE_HZ)
    if args.drive_square:
        if not 0 <= args.seconds <= MAX_LOG_SECONDS:
            raise ValueError("log time must be within 0..%g s" % MAX_LOG_SECONDS)
    elif not 0 < args.seconds <= MAX_LOG_SECONDS:
        raise ValueError("log time must be within 0..%g s" % MAX_LOG_SECONDS)
    leg_s = args.side / args.speed
    turn_s = (math.pi / 2.0) / args.turn_rate
    square_s = 4.0 * (leg_s + turn_s + 2.0 * args.stop)
    if leg_s > MAX_LEG_MOTION_S:
        raise ValueError(
            "each leg would move for %.1f s; maximum is %.1f s" % (leg_s, MAX_LEG_MOTION_S)
        )
    if turn_s > MAX_TURN_MOTION_S:
        raise ValueError(
            "each turn would move for %.1f s; maximum is %.1f s" % (turn_s, MAX_TURN_MOTION_S)
        )
    if square_s > MAX_SQUARE_MOTION_S:
        raise ValueError(
            "square motion would take %.1f s; maximum is %.1f s" % (square_s, MAX_SQUARE_MOTION_S)
        )


def yaw_from_quaternion(x, y, z, w):
    """Heading about z in radians from a unit quaternion."""
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def message_to_dict(value):
    """Plain JSON-ready data from any ROS message; times become float seconds."""
    if hasattr(value, "to_sec"):
        return value.to_sec()
    if hasattr(value, "__slots__") and hasattr(value, "_type"):
        return dict((name, message_to_dict(getattr(value, name))) for name in value.__slots__)
    if isinstance(value, (list, tuple)):
        return [message_to_dict(item) for item in value]
    if isinstance(value, bytes) and not isinstance(value, str):
        return list(bytearray(value))
    return value


class TopicLogger(object):
    """Keeps the latest sample per topic and writes them as one JSON line per tick."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.latest = {"odom": None, "status": None, "encoder": None}
        self.latest_at = {"odom": None, "status": None, "encoder": None}
        self.counts = {"odom": 0, "status": 0, "encoder": 0}
        self.ticks = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        self.out = os.fdopen(os.open(path, flags, 0o600), "w")

    def on_odom(self, msg):
        pose = msg.pose.pose
        q = pose.orientation
        record = {
            "stamp": msg.header.stamp.to_sec(),
            "frame_id": msg.header.frame_id,
            "x": pose.position.x,
            "y": pose.position.y,
            "yaw": yaw_from_quaternion(q.x, q.y, q.z, q.w),
            "vx": msg.twist.twist.linear.x,
            "wz": msg.twist.twist.angular.z,
        }
        with self.lock:
            self.latest["odom"] = record
            self.latest_at["odom"] = _monotonic()
            self.counts["odom"] += 1

    def generic_callback(self, key):
        """A callback for ``rospy.AnyMsg`` that resolves the message class at run time."""

        def callback(any_msg):
            type_name = any_msg._connection_header["type"]
            cls = roslib.message.get_message_class(type_name)
            if cls is None:
                record = {"type": type_name, "error": "message class not found on this host"}
            else:
                real = cls()
                real.deserialize(any_msg._buff)
                record = message_to_dict(real)
                record["type"] = type_name
            with self.lock:
                self.latest[key] = record
                self.latest_at[key] = _monotonic()
                self.counts[key] += 1

        return callback

    def snapshot(self, key):
        with self.lock:
            return self.latest[key]

    def current_odom(self, max_age_s, now=None):
        """Latest odometry only when it arrived recently in this process."""
        if now is None:
            now = _monotonic()
        with self.lock:
            odom = self.latest["odom"]
            received_at = self.latest_at["odom"]
        if odom is None or received_at is None:
            return None
        age = now - received_at
        return odom if 0.0 <= age <= max_age_s else None

    def write_tick(self):
        with self.lock:
            record = {"t": time.time()}
            record.update(self.latest)
            self.ticks += 1
        self.out.write(json.dumps(record) + "\n")
        self.out.flush()

    def close(self):
        self.out.close()

    def summary(self):
        return "%d ticks written; messages: odom %d, status %d, encoder %d" % (
            self.ticks,
            self.counts["odom"],
            self.counts["status"],
            self.counts["encoder"],
        )


def publish_for(pub, twist, seconds, rate, odom_is_current=None):
    end = _monotonic() + seconds
    while not rospy.is_shutdown() and _monotonic() < end:
        if odom_is_current is not None and not odom_is_current():
            raise MotionSafetyError("current odometry was lost while motion was active")
        pub.publish(twist)
        rate.sleep()


def drive_square(pub, side_m, speed, turn_rate, stop_s, rate_hz, odom_is_current=None):
    """Four legs of ``side_m`` at ``speed`` with stops and left turns; always ends stopped."""
    rate = rospy.Rate(rate_hz)
    forward = Twist()
    forward.linear.x = speed
    turn = Twist()
    turn.angular.z = turn_rate
    halt = Twist()
    try:
        for leg in range(4):
            print("leg %d: forward %.2f m at %.2f m/s" % (leg + 1, side_m, speed))
            publish_for(pub, forward, side_m / speed, rate, odom_is_current)
            publish_for(pub, halt, stop_s, rate)
            print("leg %d: turn 90 deg at %.2f rad/s" % (leg + 1, turn_rate))
            publish_for(pub, turn, (math.pi / 2.0) / turn_rate, rate, odom_is_current)
            publish_for(pub, halt, stop_s, rate)
    finally:
        for _ in range(5):
            pub.publish(halt)
            time.sleep(0.05)


def closure_error(start, end):
    """Distance and heading difference between two odometry samples."""
    dx = end["x"] - start["x"]
    dy = end["y"] - start["y"]
    dyaw = (end["yaw"] - start["yaw"] + math.pi) % (2.0 * math.pi) - math.pi
    return math.hypot(dx, dy), math.degrees(dyaw)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="odom.jsonl", help="new JSONL output file")
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="log time in seconds (required without --drive-square; max 300)",
    )
    parser.add_argument("--rate", type=float, default=10.0, help="log rate in Hz")
    parser.add_argument(
        "--drive-square", action="store_true", help="publish a square on /tb_cmd_vel"
    )
    parser.add_argument("--side", type=float, default=1.0, help="square side in metres (max 5)")
    parser.add_argument("--speed", type=float, default=0.2, help="leg speed in m/s (max 0.25)")
    parser.add_argument(
        "--turn-rate", type=float, default=0.5, help="turn rate in rad/s (max 0.75)"
    )
    parser.add_argument(
        "--stop", type=float, default=1.0, help="seconds stopped between moves (max 10)"
    )
    parser.add_argument(
        "--cmd-rate", type=float, default=10.0, help="Twist publish rate in Hz (max 50)"
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
    except ValueError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    if rospy is None:
        print(
            "rospy is not importable (%s); run inside the ROS container after "
            "'source /opt/ros/melodic/setup.bash'" % IMPORT_ERROR,
            file=sys.stderr,
        )
        return 2
    rospy.init_node("sweep_odom_log", anonymous=True)
    try:
        logger = TopicLogger(args.out)
    except OSError as exc:
        print("refused: cannot create output %s: %s" % (args.out, exc), file=sys.stderr)
        return 2
    rospy.Subscriber(ODOM_TOPIC, Odometry, logger.on_odom)
    rospy.Subscriber(STATUS_TOPIC, rospy.AnyMsg, logger.generic_callback("status"))
    rospy.Subscriber(ENCODER_TOPIC, rospy.AnyMsg, logger.generic_callback("encoder"))
    timer = rospy.Timer(rospy.Duration(1.0 / args.rate), lambda event: logger.write_tick())
    print("logging to %s" % args.out)
    start_pose = None
    result = 0
    try:
        waited = 0.0
        while (
            logger.current_odom(ODOM_MAX_AGE_S) is None
            and waited < ODOM_WAIT_S
            and not rospy.is_shutdown()
        ):
            time.sleep(0.1)
            waited += 0.1
        if logger.current_odom(ODOM_MAX_AGE_S) is None:
            message = "no current odometry after %.0f s: is tb_control running?" % ODOM_WAIT_S
            if args.drive_square:
                print("refused: %s" % message, file=sys.stderr)
                result = 2
            else:
                print(message, file=sys.stderr)
        if args.drive_square:
            if result == 0:
                pub = rospy.Publisher(CMD_VEL_TOPIC, Twist, queue_size=10)
                time.sleep(0.5)
                start_pose = logger.current_odom(ODOM_MAX_AGE_S)
                if start_pose is None:
                    raise MotionSafetyError("odometry became stale before motion started")
                drive_square(
                    pub,
                    args.side,
                    args.speed,
                    args.turn_rate,
                    args.stop,
                    args.cmd_rate,
                    lambda: logger.current_odom(ODOM_MAX_AGE_S) is not None,
                )
                time.sleep(1.0)
        else:
            deadline = None if args.seconds <= 0 else time.time() + args.seconds
            while not rospy.is_shutdown() and (deadline is None or time.time() < deadline):
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("interrupted")
    except MotionSafetyError as exc:
        print("motion aborted: %s" % exc, file=sys.stderr)
        result = 2
    finally:
        timer.shutdown()
        logger.close()
    print(logger.summary())
    end_pose = logger.snapshot("odom")
    if start_pose is not None and end_pose is not None:
        distance, heading = closure_error(start_pose, end_pose)
        print(
            "square closure error from odometry: %.3f m, %.1f deg "
            "(start x=%.3f y=%.3f yaw=%.3f; end x=%.3f y=%.3f yaw=%.3f)"
            % (
                distance,
                heading,
                start_pose["x"],
                start_pose["y"],
                start_pose["yaw"],
                end_pose["x"],
                end_pose["y"],
                end_pose["yaw"],
            )
        )
    return result


if __name__ == "__main__":
    sys.exit(main())
