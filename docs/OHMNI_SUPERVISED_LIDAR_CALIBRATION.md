# Supervised Ohmni 11 lidar calibration

The separate calibration runner captures ten settled raw lidar revolutions, moves forward about 8 cm, captures again, turns counterclockwise about 10 degrees, and captures a third time. It uses the operator's 6-inch wheel diameter and recorded Ohmni 11 mount. The host fitter estimates the lidar angle offset and handedness. Its output remains an unapproved candidate until physical evidence has been reviewed.

Field runs with the robot physically confirmed forward and left/CCW established the wheel convention: forward lowers left encoder counts and raises right counts; left/CCW raises both. Odometry negates the left delta and keeps the right delta so those motions produce positive X and positive yaw.

Further physical testing is pending. Resume only after the operator confirms the robot is on clear floor with a spotter able to stop it.

## Limits and stop behavior

The calibration entry point deliberately bypasses the normal calibrated forward-sector and obstacle checks, because their axes are unknown. A fresh raw scan, fresh paired encoders, the local control loop, a host lease, and explicit `--supervised-clear-space` are required. Ordinary driving keeps its existing guards.

Commands are fixed at 0.04 m/s forward or 10 degrees/s yaw, one axis at a time, in pulses of at most 0.5 seconds. The default profile stops after 0.18 m accumulated mean absolute wheel travel, 15 degrees accumulated absolute yaw, or 60 seconds. `--longer-calibration` selects the only other supported immutable profile: 0.40 m forward, 30 degrees counterclockwise yaw, 0.60 m wheel travel, 40 degrees accumulated yaw, and the same speed, pulse, lease, raw-scan, stationary-stage, and 60-second bounds. The fitter accepts only either complete profile. A pulse producing less than 1 mm translation or 0.25 degrees yaw stops the run. The runner also aborts on stale sensing, more than 1 mm or 0.1 degrees of drift during a capture stage, a stalled owner loop, or lease loss. Host renewals arrive every 100 ms and expire after 350 ms; the local control tick checks the deadline.

Closing the lease host or interrupting the foreground runner requests a stop. Retain the physical stop option throughout: a software deadline depends on the controller and motor interface remaining responsive. A failed run removes its capture. A completed capture is written only after disable returns successfully.

## Prepare offline

Use the reviewed `integration/ohmni-self-calibration` checkout. Run the Ohmni adapter tests, host lease tests, fitter tests, and runner-to-fitter pipeline tests. Package its `adapters/**/*.py` files into a new archive, preserving paths and excluding bytecode. Record the commit, archive SHA-256, and source digest:

```sh
.venv/bin/python -c 'from adapters.ohmni.calibration import calibration_source_sha256; print(calibration_source_sha256())'
```

The source digest includes each relative Python path and its file digest in sorted order. It covers the complete adapters tree, including the runner and its local dependencies. The capture command recomputes this digest and compares it with `--expected-source-sha256` before constructing the device. Keep the extracted tree private and immutable during the run, with no pre-existing bytecode.

## Resume at the robot

1. Obtain the operator's clear-floor and spotter confirmation. Check the current boot ID, native encoder publisher health, and absence of another Sweep drive owner. Preserve the vendor motor and encoder owner. Stop an existing Sweep node through its recorded PID and supported stop command, then verify it exited.
2. Extract the reviewed archive into a new private `/data/local/tmp/sweep-lidar-calibration-<source-digest>` directory. Preserve `/data/local/sweep`, its runtime, and `node.env`. Use the installed musl loader and Python interpreter with the new directory as both working directory and `PYTHONPATH`. Set `PYTHONDONTWRITEBYTECODE=1` and use `-B`.
3. Generate a fresh 32-byte random lease token as 64 hexadecimal characters in a mode-0600 file. Copy it into the private device directory without printing it. Set up an ADB reverse mapping for device TCP 18912 to host TCP 18912. Start `tools.ohmni_supervised_lidar_calibration` on the host with `--token-file`. It accepts one authenticated loopback client and closes within 60 seconds of startup.
4. Run the node entry point in the foreground. Substitute the observed boot ID, reviewed source digest, token path, and a new capture path into the command below. Starting this command enables the fixed motion sequence.

```sh
cd <private-extracted-directory>
export PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1
/data/local/sweep/lib/ld-musl-x86_64.so.1 \
  /data/local/sweep/python/bin/python3.12 -B -m adapters.ohmni.calibration \
  --lease-port 18912 \
  --lease-token-file <private-token-path> \
  --expected-boot-id <observed-boot-id> \
  --expected-source-sha256 <reviewed-source-digest> \
  --supervised-clear-space \
  --output <new-private-capture-path>
```

5. Confirm the runner exited and the drive stopped. Close the host lease, remove the specific ADB reverse mapping, and remove the token copies. Retrieve the completed capture and logs. Hash the capture and retain the exact source archive beside it. A reboot, disconnect, refusal, or unconfirmed stop ends this attempt; investigate before another launch.

## Fit and review

Run the host-only fitter with new output paths:

```sh
.venv/bin/python -m tools.ohmni_lidar_self_calibration capture.json candidate.json
```

Review the capture's boot/source pins, measured motion and stage drift, fit and held-out residuals, competing basins, and offset uncertainty. Inspect transformed scans against the room and confirm the proposed forward direction. The fitter writes no robot settings. Updating normal lidar configuration and accepting motion remain separate field steps after that review.
