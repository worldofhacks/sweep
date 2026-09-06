#!/usr/bin/env python3
"""Drive a square over the bot shell and record command-to-reply latency.

    python3 drive_probe.py --dry-run
    python3 drive_probe.py --side 1.0 --speed 10 --log drive.jsonl

Sequence: ``battery``, ``start_collision_detection``, ``init``, then for each leg
``pre_drive <side_mm> <speed>`` and ``pre_rot 90 <speed>`` (``pre_drive`` takes millimetres
and ``pre_rot`` degrees, positive to the right; both take a speed of 0..20). Neither command
reports completion, so each leg waits a fixed time while polling ``apos 0`` and ``apos 1``
(the wheel servos' status; its output is undocumented) and logging every reply with its send
and first-byte times. The bot shell has no odometry read command: drift over the square comes
from ``ros_odom_log.py`` in the ROS container, or from the tape measure. On any exit the probe
sends ``manual_move 0 0`` and then ``sleep``. Python 3.6, standard library only.
"""

import argparse
import json
import statistics
import sys
import time

try:
    from . import botshell
except ImportError:  # run as a script from the spike directory
    import botshell

MAX_SPEED = 20
MAX_SIDE_M = 5.0
MAX_LEGS = 4
MAX_SETTLE_S = 10.0
MAX_LEG_WAIT_S = 60.0
MAX_TURN_WAIT_S = 30.0
MAX_PLAN_WAIT_S = 180.0
MAX_POLL_HZ = 20.0
POLL_SIDS = (0, 1)


def _finite(value):
    return not (value != value or value in (float("inf"), float("-inf")))


def validate_args(args):
    """Refuse plans outside the deliberately small hardware-spike envelope."""
    numeric = {
        "side": args.side,
        "settle": args.settle,
        "leg wait": args.leg_wait,
        "turn wait": args.turn_wait,
        "poll rate": args.poll_hz,
    }
    for name, value in numeric.items():
        if not _finite(value):
            raise ValueError(f"{name} must be finite")
    if not 1 <= args.speed <= MAX_SPEED:
        raise ValueError(f"speed {args.speed} outside 1..{MAX_SPEED}")
    if not 0 < args.side <= MAX_SIDE_M:
        raise ValueError(f"side must be within 0..{MAX_SIDE_M:g} m")
    if not 1 <= args.legs <= MAX_LEGS:
        raise ValueError(f"legs must be within 1..{MAX_LEGS}")
    limits = (
        ("settle", args.settle, MAX_SETTLE_S),
        ("leg wait", args.leg_wait, MAX_LEG_WAIT_S),
        ("turn wait", args.turn_wait, MAX_TURN_WAIT_S),
        ("poll rate", args.poll_hz, MAX_POLL_HZ),
    )
    for name, value, maximum in limits:
        if not 0 < value <= maximum:
            raise ValueError(f"{name} must be within 0..{maximum:g}")
    botshell.validate_timeout(args.timeout)
    wait_s = args.settle + args.legs * (args.leg_wait + args.turn_wait)
    if not args.no_collision_detection:
        wait_s += 0.5
    if wait_s > MAX_PLAN_WAIT_S:
        raise ValueError(f"planned waits total {wait_s:g} s; maximum is {MAX_PLAN_WAIT_S:g} s")


def build_plan(args):
    """The ordered (command, wait_seconds) list; ``--dry-run`` prints exactly this."""
    plan = [("battery", 0.0)]
    if not args.no_collision_detection:
        plan.append(("start_collision_detection", 0.5))
    plan.append(("init", args.settle))
    side_mm = int(round(args.side * 1000))
    angle = -90 if args.ccw else 90
    for _ in range(args.legs):
        plan.append((f"pre_drive {side_mm} {args.speed}", args.leg_wait))
        plan.append((f"pre_rot {angle} {args.speed}", args.turn_wait))
    plan.append(("battery", 0.0))
    return plan


class DriveLog:
    def __init__(self, path):
        self.handle = open(path, "x") if path else None
        self.latencies = []
        self.silent = 0

    def record(self, step, reply, kind):
        entry = reply.as_record()
        entry["step"] = step
        entry["kind"] = kind
        if self.handle is not None:
            self.handle.write(json.dumps(entry) + "\n")
            self.handle.flush()
        if kind == "command":
            if reply.latency_s is None:
                self.silent += 1
            else:
                self.latencies.append(reply.latency_s * 1000.0)

    def close(self):
        if self.handle is not None:
            self.handle.close()

    def summary(self):
        if not self.latencies:
            return f"no command replies measured ({self.silent} silent)"
        return (
            f"command-to-first-byte over {len(self.latencies)} replies: "
            f"min {min(self.latencies):.1f} ms, median {statistics.median(self.latencies):.1f} ms, "
            f"max {max(self.latencies):.1f} ms; {self.silent} silent"
        )


def poll(shell, log, step, seconds, hz):
    """Poll the wheel servo status during a leg; every reply is logged as ``kind: poll``."""
    if hz <= 0:
        time.sleep(seconds)
        return
    interval = 1.0 / hz
    deadline = time.monotonic() + seconds
    while True:
        tick = time.monotonic()
        if tick >= deadline:
            break
        for sid in POLL_SIDS:
            reply = shell.command(f"apos {sid}", timeout=min(0.2, interval / 2))
            log.record(step, reply, "poll")
            if reply.lines:
                print(f"  apos {sid}: {' | '.join(reply.lines)}")
        remaining = deadline - time.monotonic()
        time.sleep(max(0.0, min(interval - (time.monotonic() - tick), remaining)))


def execute(args, plan):
    log = DriveLog(args.log)
    shell = botshell.BotShell(args.sock, args.timeout)
    connected_once = False
    try:
        shell.connect()
        connected_once = True
        for step, (command, wait) in enumerate(plan):
            reply = shell.command(command)
            botshell.print_reply(reply)
            log.record(step, reply, "command")
            if wait > 0:
                poll(shell, log, step, wait, 0.0 if args.no_poll else args.poll_hz)
    finally:
        if connected_once:
            stop_and_sleep(shell)
        shell.close()
        log.close()
    print(log.summary())
    if args.log:
        print(f"log: {args.log}")


def stop_and_sleep(shell):
    """Always leave the wheels stopped and disabled, reconnecting once if the socket dropped."""
    for attempt in range(2):
        try:
            if not shell.connected:
                shell.connect()
            botshell.print_reply(shell.stop())
            botshell.print_reply(shell.command("sleep"))
            return
        except botshell.BotShellError as exc:
            print(f"stop attempt {attempt + 1} failed: {exc}", file=sys.stderr)
    print("WHEELS NOT CONFIRMED STOPPED: use the robot's screen or power switch", file=sys.stderr)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sock", default=botshell.DEFAULT_SOCK, help="bot shell socket")
    parser.add_argument("--timeout", type=float, default=botshell.DEFAULT_TIMEOUT)
    parser.add_argument("--side", type=float, default=1.0, help="square side in metres (max 5)")
    parser.add_argument("--speed", type=int, default=10, help="pre_drive/pre_rot speed 1..20")
    parser.add_argument("--legs", type=int, default=4, help="drive-and-turn repetitions (max 4)")
    parser.add_argument("--ccw", action="store_true", help="turn left instead of right")
    parser.add_argument("--settle", type=float, default=1.0, help="seconds after init (max 10)")
    parser.add_argument(
        "--leg-wait", type=float, default=8.0, help="seconds per pre_drive (max 60)"
    )
    parser.add_argument("--turn-wait", type=float, default=5.0, help="seconds per pre_rot (max 30)")
    parser.add_argument("--poll-hz", type=float, default=2.0, help="apos polling rate")
    parser.add_argument("--no-poll", action="store_true", help="do not poll apos while waiting")
    parser.add_argument(
        "--no-collision-detection", action="store_true", help="skip start_collision_detection"
    )
    parser.add_argument("--log", help="create a new JSONL file for command and poll replies")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; do not connect")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    plan = build_plan(args)
    print("plan:")
    for command, wait in plan:
        print(f"  {command:<40} then wait {wait:.1f} s")
    if args.dry_run:
        print("dry run: nothing sent")
        return 0
    try:
        execute(args, plan)
    except KeyboardInterrupt:
        print("interrupted; wheels stopped and slept in cleanup")
        return 130
    except FileExistsError as exc:
        print(f"refused: log file already exists: {exc.filename}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except botshell.BotShellError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
