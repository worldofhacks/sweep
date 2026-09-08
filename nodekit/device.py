"""What a vehicle has to implement to join a Sweep session.

``Node`` owns the wire, the watchdog, and command admission; a ``Device`` owns motion and
the hardware truth behind it. The seam is deliberately narrow: everything the relay,
planner, arbiter, and console need arrives through ``status()`` and the four motion
methods, with explicit class-specific capabilities and hardware evidence.

Nothing here talks to a socket, so an integration can be exercised against the shapes in
``nodekit.fake`` in a unit test before the hardware exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

# Device classes on the wire; ``planner.models.DeviceClass``.
AIRCRAFT = "aircraft"
GROUND_VEHICLE = "ground_vehicle"

# ``planner.models.FlightState`` values an aircraft may report in telemetry.
FLIGHT_STATES = (
    "disarmed",
    "landed",
    "armed",
    "taking_off",
    "airborne",
    "hovering",
    "landing",
    "emergency",
)

# ``planner.models.DriveState`` values a ground vehicle may report in telemetry.
DRIVE_STATES = ("docked", "idle", "moving", "stopped", "fault")


@dataclass
class DeviceStatus:
    """One reading of where the device is and how well it is doing.

    Positions are metres in the frame the device's telemetry uses (for a ground vehicle,
    wheel odometry anchored at its launch spot), ``yaw_deg`` is counter-clockwise from +x,
    and ``battery``, ``link``, and ``pos_quality`` are 0..1 fractions. ``state`` is a
    ``FLIGHT_STATES`` or ``DRIVE_STATES`` value for the device's class; a ground vehicle
    reports ``z`` and ``vz`` as 0.
    """

    x: float
    y: float
    z: float
    yaw_deg: float
    vx: float
    vy: float
    vz: float
    battery: float
    link: float
    pos_quality: float
    state: str
    control_authority: bool
    # Bounded JSON readings published in node_status.device_telemetry.
    # Missing readings use null; these informational facts never grant authority.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scan:
    """One full revolution of a scanning range sensor, in the device's own frame.

    ``pose`` is (x, y, yaw_deg) at scan time in the same frame as telemetry. Angle 0 points
    along the device's forward axis and angles increase counter-clockwise in steps of
    ``angle_increment_deg`` (0.5, 1.0, or 2.0), so ``ranges_cm`` holds exactly
    360 / increment entries, each centimetres with 0 meaning no return.
    """

    t_ms: int
    pose: tuple[float, float, float]
    angle_min_deg: float
    angle_increment_deg: float
    range_min_m: float
    range_max_m: float
    ranges_cm: list[int]


class Device(Protocol):
    """The vehicle behind a node.

    ``device_class`` is ``aircraft`` or ``ground_vehicle`` and ``capabilities`` is the
    device's own claims *without* the ``class:`` entry, which the kit adds. A ground
    vehicle must claim ``ground_drive`` and an aircraft ``flight`` before the relay will
    call it ready; ``lidar``, ``camera``, ``neck``, ``speech``, ``lights``, and ``screen``
    are optional and drive what the console offers.

    The motion methods return a motion id that ``motion_done`` resolves, so a device is
    free to run its own control loop; a device that arrives instantly returns an id whose
    ``motion_done`` is already ``True``.

    Two members are optional and read with ``getattr``: ``video_publish_state()`` returns
    ``stopped``, ``connecting``, ``publishing``, or ``failed`` for the camera publisher
    (``stopped`` when the device has none), and ``close()`` releases hardware when the node
    stops.
    """

    device_class: str
    capabilities: Sequence[str]

    def status(self) -> DeviceStatus:
        """The current reading; called at the telemetry rate, so it must not block."""

    def stop(self) -> None:
        """Hold position and stay enabled: the watchdog hold and the ``hover`` command."""

    def disable(self) -> None:
        """Failsafe: wheels off, motors safe. Only a re-enable undoes it."""

    def enable(self) -> bool:
        """Take control authority; return whether the device actually granted it."""

    def move_to(self, x_m: float, y_m: float, z_m: float, speed_m_s: float) -> str:
        """Start driving or flying to a pose in the telemetry frame; return a motion id."""

    def rotate_to(self, yaw_deg: float, speed_deg_s: float) -> str:
        """Start turning to an absolute heading; return a motion id."""

    def motion_done(self, motion_id: str) -> bool | None:
        """``True`` when that motion finished, ``False`` while it runs, ``None`` if it failed."""

    def latest_scan(self) -> Scan | None:
        """The newest scan, or ``None`` when the device has no scanning sensor."""

    def hardware_profile(self) -> dict[str, Any]:
        """The ``capabilities`` frame fields this device knows
        (``nodekit.protocol.CAPABILITIES_PROFILE_FIELDS``); the rest take the kit defaults."""


def scan_is_valid(scan: Scan) -> bool:
    """Whether a scan can go on the wire at all: its bin count against its increment."""
    if scan.angle_increment_deg not in (0.5, 1.0, 2.0):
        return False
    return len(scan.ranges_cm) == int(round(360 / scan.angle_increment_deg))
