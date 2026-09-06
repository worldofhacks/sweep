# Ohmni LiDAR commissioning

Use the single console at `http://127.0.0.1:5173/`. A live camera or a healthy
scanner does not by itself qualify motion. The current robot adapter grants
authority only while its local LiDAR clearance checks pass. Console Arm cannot
repair absent hardware, unknown scan sectors, or an obstruction.

## Verified baseline — 2026-09-06, 23:22 UTC

All three deployed nodes and readers matched `d806a71` byte for byte. A passive
16-second relay capture recorded 53 scans each from G-01 and G-02. No control
commands were sent.

| Robot | Hardware | Captured scan evidence | Remaining work |
| --- | --- | --- | --- |
| G-01 | A2M8, firmware 1.25, startup health good | Nearest nonzero return 14 cm in all 53 frames; only 9–10 of 12 sectors observed; sectors 30–89° empty throughout and 0–29° empty in 52 frames | Check the scan plane for obstructions and mounting interference; investigate intermittent stream corruption |
| G-02 | A2M8, firmware 1.25, startup health good | Nearest return 17 cm; all 12 sectors observed in all 53 frames | Identify and clear the close return; investigate the less frequent stream fault |
| G-03 | No LiDAR USB adapter or expansion hub detected; hardware handoff records no kit installed | No scans | Install and commission a compatible scanner kit with its USB/power hardware |

These are **sensor angles**, not measured robot headings. Mounting yaw has not
been qualified. A 14 cm return is below the published 15 cm measurement minimum:
it is close-return evidence, not an accurate clearance measurement and not free
space. Do not discard it to make readiness pass.

The extended logs add a separate reliability issue. From roughly 22:28–23:25 UTC,
G-01 recorded five lost-revolution and four invalid-angle errors; G-02 recorded
one lost-revolution error. The latest G-01 failure was at 23:22:27.042 UTC:

```text
previous=58.016 angle=60.375 start=False sweep=421.969
```

Fresh healthy scans resumed at 23:22:31.545 UTC. Automatic recovery works, but
the recurring acquisition fault remains unresolved. Near returns and missing
coverage persisted outside these recovery intervals.

## Physical checks, then a fresh comparison

1. With drive stopped, park G-01 and G-02 with at least 60 cm of clearance around
   each scanner at its scan height. Check cables, brackets, the robot body, and
   nearby objects intersecting that plane. Check the scanner window for debris.
   The 60 cm setup margin exceeds the current 45 cm local stop threshold; it is
   not a complete collision-safety qualification.
2. Record a new stationary scan capture. Compare each 30° sector and close-return
   arc with the baseline. A persistent arc after the surroundings are cleared
   needs measured inspection of the mount and scan plane. Do not infer a body
   exclusion mask from the distance alone: excluded/occluded space stays unknown.
3. If missing sectors persist in an open area, distinguish occlusion from
   out-of-range or poorly returned surfaces using a measured, stationary target
   in those sectors. Perform this with drive disabled and no movement commands.
4. Investigate USB cable, connector, and powered-hub condition on G-01, especially
   because its error rate is higher. Coordinate any unplugging or driver restart
   with the active hardware owner so only one process owns the scanner. Recheck
   longer than the previous few-minute failure intervals after changing one
   component at a time.
5. G-03 needs its own scanner kit for three robots to have LiDAR concurrently.
   Moving a kit from another robot only moves the hardware gap. Verify the
   adapter by USB identity and confirm the A2M8 model and healthy scan stream;
   never substitute the FT230X wheel UART for a missing LiDAR port.

## Driver follow-up

The deployed normal-scan bitfields and distance conversion agree with Slamtec's
[measurement definitions](https://github.com/Slamtec/rplidar_sdk/blob/master/sdk/include/sl_lidar_cmd.h)
and [normal-scan decoder](https://github.com/Slamtec/rplidar_sdk/blob/master/sdk/src/dataunpacker/unpacker/handler_normalnode.cpp).
An offline reproduction produced the exact failure above by omitting a scan-start
marker. Restoring that marker on either a zero-range or zero-quality packet reset
the scan correctly. This rules out those particular parser hypotheses; it does
not prove where the live marker or angle sequence went wrong.

The active driver owner has the exact fault timestamps and sanitized logs. The
next software change should retain bounded raw packet history on failure,
including start flags, quality, angles, ranges, accumulated rotation, serial
backlog and monotonic timing. Use the resulting evidence for a regression test
before changing parsing. Retain freshness withdrawal and automatic recovery.

Detailed LiDAR reasons in the console are already owned by the concurrent
integration agent. Keep one canonical console and distinguish missing hardware,
stale/invalid scans, incomplete coverage and close obstacles. A generic
`control_authority_missing` label is insufficient for diagnosing these cases.

## Completion evidence

- Installed hardware and firmware identified for each robot requiring LiDAR.
- A stationary soak longer than the observed fault intervals, with fresh scans,
  no unexplained acquisition errors and stable coverage in the test area.
- A measured target at known bearings verifies range and mounting yaw; an
  obstruction withdraws readiness and removing it restores genuine clearance.
- Unplugged, stale, incomplete and corrupted scans withhold motion; recovery
  requires fresh evidence rather than a console override.
- Only then perform bounded drive/stop checks with the hardware owner and local
  operator. A single-plane scanner still cannot observe objects outside its
  scan height. Multi-robot motion additionally needs the qualified shared frame,
  local stop and command/acknowledgement work tracked in issues #94, #239 and #246.

This baseline diagnoses the current blocks. It does not mark LiDAR reliability,
mount calibration or physical robot control as accepted.
