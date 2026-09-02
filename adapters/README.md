# adapters

Capability area: Autonomy. Milestones: M1 (`sim`), M2 (hardware).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the adapter interface name one change owner and require cross-review.

One interface, three implementations (PRD Appendix C):

| Package | Target | Milestone |
|---|---|---|
| `sim/` | kinematic, deterministic simulator, two drones for M2.0 and then 4 to 6; the first-class mock used by CI and the console before hardware | M1 |
| `crazyswarm2/` | ROS 2 Crazyflie server (takeoff, go_to, land, notify_setpoints_stop, emergency) with Lighthouse or Loco positioning | M2 |
| `mavlink/` | pymavlink or MAVSDK for PX4 or ArduPilot quads | optional |

Interface: `takeoff, goto, hover, land, estop, telemetry`. Hardware is a configuration flag; every feature is built and tested against `sim` first.

CI has no ROS 2. Tests that import `rclpy` or the crazyswarm2 interfaces must guard with `pytest.importorskip("rclpy")` or run where ROS is on the path.

PRD: sections 4.5, 5.6.
