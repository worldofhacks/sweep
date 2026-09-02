# adapters

Capability area: Autonomy. Milestones: M1 (`sim`), M2 (hardware).

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to the adapter interface name one change owner and require cross-review.

One interface, two implementations (PRD Appendix C):

| Package | Target | Milestone |
|---|---|---|
| `sim/` | kinematic, deterministic simulator, two drones for M2.0 and then 4 to 6; the first-class mock used by CI and the console before hardware | M1 |
| DJI Mini 3 bridge | one Android node per Mini 3 and RC-N1 pair via the DJI Mobile SDK, proven on one exact hardware combination before duplication | M2 |

Interface: `takeoff, goto, hover, land, estop, telemetry`, plus the negotiated `CameraCapture` capability. Hardware is a configuration flag; every feature is built and tested against `sim` first.

PRD: sections 4.5, 5.6, Appendix C.
