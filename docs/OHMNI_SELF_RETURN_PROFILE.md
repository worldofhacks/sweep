# Unit 12 LiDAR self-return candidate

The Unit 12 candidate is inactive, and production configuration has no default profile. It cannot remove a return until a physical qualification supplies an expiry and is bound to the same Unit 12 boot, LiDAR calibration, and measured mount transform. The raw revolution always retains every decoded return.

The current candidate records an observation, not an acceptance decision. The 0.181 m rear family appeared in all ten stationary revolutions of baseline captures 4 and 5. Its medians were 178.921875 degrees and 181.375 mm before the manual move, then 178.890625 degrees and 181.125 mm after it. Baseline 6 recorded 179.078125 degrees and 180.0 mm. The corresponding body-local position stayed near `[-0.098, 0.022]` m while the robot moved about 0.57 m. The particular reflecting part and its occlusion region remain unknown.

The retained candidate is deliberately restricted to the repeated rear family. It does not include the intermittent 0.255 m, 0.340 m, or 0.556 m families. The room-fixed return near raw angle 309 degrees moved from 0.735 m to 1.304 m after the manual translation and remains available to mapping and safety.

The serial diagnostic replayed raw bytes with SHA-256 `62032f838479c2cbb4f85d4ee6af7c2350e090d021c00c51c03d3f03fe895579` and recorded zero parser resynchronizations. It rules out a parser recovery explanation for this capture, while leaving the physical cause unresolved.

## Runtime behavior

An operator or deployment integration must pass a profile explicitly to `Config`. A profile only filters a raw point when all of these facts hold:

- the profile is enabled and has a physical-qualification ID;
- the qualification has not expired on the LiDAR monotonic clock;
- the unit ID, source boot ID, raw-angle calibration, and three-dimensional mount match the profile; and
- the point is inside one of the profile's measured bands.

A mismatch or expired qualification leaves the point in the derived scan. A filtered ray becomes an unknown scan bin. The existing full-circle ground guard refuses unknown bins, so filtering cannot create free space. A valid physical qualification must establish that the selected ray lies inside the robot's body or an occlusion region where an external return cannot be distinguished from the qualified self-return.

## Physical acceptance pending

Physical acceptance requires a measured LiDAR-plane envelope for the robot and mounts, repeated scans with and without each candidate surface in view, and a review of the proposed occlusion region. It must also produce a fresh qualification bound to the boot and calibration that will run it. The observational Unit 12 data does not yet meet that bar.
