# adapters

Owner: B (Autonomy).

One interface, three implementations (PRD Appendix C):

| Package | Target | Phase |
|---|---|---|
| `sim/` | kinematic, deterministic six-drone simulator; the first-class mock used by CI and the console before hardware | 1 |
| `crazyswarm2/` | ROS 2 Crazyflie server (takeoff, go_to, land, notify_setpoints_stop, emergency) with Lighthouse or Loco positioning | 2 |
| `mavlink/` | pymavlink or MAVSDK for PX4 or ArduPilot quads | optional |

Interface: `takeoff, goto, hover, land, estop, telemetry`. Hardware is a configuration flag; every feature is built and tested against `sim` first.

PRD: sections 4.5, 5.6.
