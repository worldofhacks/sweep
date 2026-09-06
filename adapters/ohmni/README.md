# Ohmni telepresence robots

Capability area: Platform, with Autonomy and Interaction. Issue #239, slice S0; plan F.2
(extend vehicle portability).

This directory holds the S0 hardware spike for the Ohmni telepresence robots, the first
non-aircraft device family. On one robot at a time it answers the questions S1 to S3 need
before any relay, planner, arbiter, or console change: how a developer unit is reached, which
drive path gives usable latency and odometry, which robots carry the lidar kit and how its
scans come out, and how the camera gets off the robot and what encoding costs the Atom. Nothing
here touches the relay. Results go into the table at the end of this file, and the parsers
under `spike/` are written so the S3 node can import them unchanged.

Every hardware statement below comes from the vendor documentation until a robot has shown
it. Bot shell reply formats, the unit of `manual_move` speeds, and the camera format code are
not documented anywhere and are recorded by the operator.

## Layout

| File | Purpose |
|---|---|
| `spike/botshell.py` | `BotShell` client for `/app/bot_shell.sock` and a CLI: `battery`, `say`, `raw`, `stop`, and a bounded `pulse` |
| `spike/rplidar_protocol.py` | RPLIDAR A2 request frames, descriptor, info, health, and scan parsing; no I/O |
| `spike/lidar_probe.py` | opens the lidar serial port with `termios`, prints info and health, streams revolutions, records JSONL |
| `spike/ohmnicam.py` | `OHMNICAM` datagram parsing and frame reassembly; no I/O |
| `spike/camera_probe.py` | binds `/dev/libcamera_stream`, measures resolution and frame rate, records raw frames, publishes over RTSP through `ffmpeg` |
| `spike/drive_probe.py` | drives a square over the bot shell and logs command-to-reply latency |
| `spike/ros_odom_log.py` | logs `/tb_control/wheel_odom` and core status from the ROS container; drives a square over `/tb_cmd_vel` |
| `spike/push.sh` | pushes `spike/` to a robot over adb and prints the in-container commands |

All Python under `spike/` runs on the container's stock Python 3.6 with the standard library
only; `ros_odom_log.py` also runs on the ROS image's Python 2. `tests/adapters/ohmni/` covers
the parsers and the bot shell framing without hardware.

## The robot, as documented

- Ohmni OS is Android-x86 on an Intel Atom x5-Z8350 UP board. The telebot app is
  `com.ohmnilabs.telebot_rtc`; its files directory
  `/data/data/com.ohmnilabs.telebot_rtc/files` is `/app` inside the developer container.
- Developer edition: ADB over Wi-Fi on port 5555 after enabling it in the app settings; `su`
  gives root; SSH as root with a key added in the bot's developer settings.
- `dockerenv` (as root) starts the `ohmnilabs/ohmnidev` container: Ubuntu 18.04, Python 3.6.7
  and 2.7, Node 10, gcc. `apt install` works but does not survive the next `dockerenv`.
  `docker-ohmnirun <image>` runs any image with the same privileged flags. Volumes:
  `/home/ohmnidev` is `/var/dockerhome` on Android, `/app` is the telebot files directory,
  `/dev` is `/dev`.
- Bot shell: newline-terminated text commands on the UNIX stream socket `/app/bot_shell.sock`.
  Movement: `manual_move <lspeed> <rspeed>` (does not stop by itself), `pre_drive <mm>
  <speed 0..20>`, `pre_rot <deg, positive right> <speed 0..20>`; `init` and `sleep` enable and
  disable the wheels and neck; `battery`, `battery_query`, `voltage <sid>`, `apos <sid>`
  (component status; sid 0 and 1 are the wheels, 3 the neck, 20 the LED);
  `start_collision_detection`, `stop_collision_detection`; `say`, `light_color`, `pos`; lidar
  `scan_lidar_device`, `lidar_scan`, `lidar_stop`, `lidar_set_pwm <0..1023>`, `lidar_release`.
  No command reads odometry or encoders, and no reply format is documented.
- Lidar kit (not on every robot): Slamtec RPLIDAR A2M8 on USB serial through the USB expansion
  hub, which has its own 5 V supply. The device path is `serialport` in
  `/app/telebot_config.json`, default `/dev/usb/tty1-2.1` (paths under `/dev/usb` follow the
  physical port). The vendor's reference driver speaks Slamtec's serial protocol directly at
  115200 8N1 and clears DTR on connect.
- Camera: while any app holds the camera, the HAL sends grayscale frames as datagrams to a UNIX
  datagram socket that the reader itself binds at `/dev/libcamera_stream`.
- ROS (developer edition): `ohmnilabsvn/ohmni_ros:ohmni_ros_tbcontrol_0.0.13`, ROS Melodic,
  publishes `/tb_control/wheel_odom` (`nav_msgs/Odometry`, 50 Hz), `/tb_control/wheel_encoder`
  (`motor_left_vel`, `motor_right_vel`, `motor_left_pos`, `motor_right_pos`, 50 Hz), and
  `/tb_control/tbcore_status` (`voltage_cell_1` to `voltage_cell_5`, `charging_current`,
  `docked_status`, 10 Hz); subscribes `/tb_cmd_vel` (`geometry_msgs/Twist`), `/cmd_vel_accel`,
  and `/tb_cmd_motor_pwm` (-1000..1000).

## Runbook

Per robot, in this order. Keep a spotter beside the robot with its screen within reach. Every
drive step ends with the wheels stopped and `sleep` sent; `python3 botshell.py stop` or the
power switch is the fallback.

On the laptop: Android platform-tools (`adb`), `ffmpeg`, this repo; the robot and the laptop
on the flight-room Wi-Fi; MediaMTX from `just media` for the publish step (`media/README.md`
for the `ground1` path and its publish password); a clear floor of about 2 m by 2 m with the
start pose taped (a 6 m by 6 m floor for the 5 m square the issue asks for).

1. Enable developer access. In the Ohmni app on the robot's screen open settings, tap
   "Version Name" seven times, scroll to the bottom, and turn on "Enable ADB". Record the
   model, the software version, and the edition from the same screen.
2. Connect and push:

   ```
   adb connect <robot_ip>:5555
   adb devices
   adapters/ohmni/spike/push.sh <robot_ip>
   ```

   `push.sh` copies `spike/` to `/var/dockerhome/sweep-spike` and prints the in-container
   commands. If the direct push is refused it stages under `/data/local/tmp` and prints the
   root-shell copy to run.
3. Root shell and container:

   ```
   adb -s <robot_ip>:5555 shell
   su
   docker images
   dockerenv
   cd /home/ohmnidev/sweep-spike
   python3 --version
   ```

   Record the `ohmnilabs/ohmnidev` image tag from `docker images` and the Python version. If
   `su` is refused the robot is not a developer edition: record that and stop; only the WebAPI
   standalone page applies to it.
4. Bot shell:

   ```
   python3 botshell.py battery
   python3 botshell.py say "sweep spike"
   python3 botshell.py raw scan_lidar_device
   python3 botshell.py raw "apos 0"
   ```

   Record every reply line verbatim: the charge and docked fields, the `scan_lidar_device`
   output (this is the lidar-kit answer), and whether `apos` says anything about wheel
   position. A command that prints `no reply` within the timeout is silent, not failed;
   `--timeout 2` waits longer.
5. Drive over the bot shell. The robot must be undocked with the taped start pose under its
   centre.

   ```
   python3 drive_probe.py --dry-run
   python3 drive_probe.py --log drive.jsonl
   python3 drive_probe.py --side 5 --leg-wait 30 --log drive5m.jsonl
   ```

   The probe sends `battery`, `start_collision_detection`, `init`, then four times
   `pre_drive 1000 10` and `pre_rot 90 10`, polling `apos 0` and `apos 1` while each leg runs,
   and always ends with `manual_move 0 0` and `sleep`. Record the latency summary (command to
   first reply byte; "silent" means nothing came back within the timeout), whether the robot
   finished each leg inside the waits (`--leg-wait` and `--turn-wait` adjust them), and the
   tape-measured closure error between the start pose and the end pose. To learn the
   `manual_move` unit, run one bounded pulse and measure the distance:
   `python3 botshell.py pulse 200 200 500`.
6. Lidar, on robots whose `scan_lidar_device` reply shows a device:

   ```
   ls -l /dev/usb/
   cat /app/telebot_config.json
   python3 lidar_probe.py --seconds 10 --record scans.jsonl
   ```

   The probe sends `lidar_release` over the bot shell, opens the port raw at 115200 8N1,
   resets, prints the info and health replies, spins the motor at PWM 660, and prints one line
   per revolution. Record the device path, the model and firmware from the info line, health,
   revolutions per second, and points per revolution, then copy `scans.jsonl` back with
   `adb pull`. Each JSONL record is one revolution: `t` (epoch seconds), `angles_deg`,
   `ranges_m` (0.0 for no return), and `qualities`. If the info request times out, check that
   `lidar_release` answered and that nothing else holds the port
   (`ls -l /proc/*/fd 2>/dev/null | grep tty` inside the container).
7. Camera. Hold the camera open first: start a call to the robot from the Ohmni web app, or
   load a standalone WebAPI page that calls `getUserMedia`; record which. Then, inside the
   container:

   ```
   python3 camera_probe.py --seconds 10
   python3 camera_probe.py --seconds 30 --out frames.raw
   apt-get update && apt-get install -y ffmpeg
   python3 camera_probe.py --seconds 60 --publish rtsp://<user>:<password>@<laptop_ip>:8554/ground1
   ```

   Record resolution, format code, frame size, mean fps, and dropped frames. For the publish
   run record the `ffmpeg` CPU percentage the probe prints (one core is 100) and the
   end-to-end latency: point the robot at a stopwatch on a screen, play `ground1` on the
   laptop, photograph both, and subtract. Encode the raw file with the `ffmpeg` line the probe
   prints. `apt-get` does not persist across `dockerenv`, so reinstall per session or run a
   `docker-ohmnirun` image that ships `ffmpeg`. The other camera path, `getUserMedia` plus
   WHIP from the standalone page, is measured the same way from the console's live tile;
   record which path wins and why.
8. Wi-Fi. From the laptop, `ping -c 60 <robot_ip>` while the publish run is up; on the robot,
   `adb shell dumpsys wifi | grep -i rssi`. Record loss and RSSI.
9. Leave the robot stopped: `python3 botshell.py stop`, `python3 botshell.py raw sleep`, then
   dock it.

## ROS odometry

The second drive path. On the robot, as root, outside `dockerenv`:

```
docker run -it --network host --privileged -v /dev:/dev -e ROS_IP=<robot_ip> ohmnilabsvn/ohmni_ros:ohmni_ros_tbcontrol_0.0.13
```

Inside the image `tb_control` runs in a tmux session (`tmux attach -t work`). In another shell
`source /opt/ros/melodic/setup.bash`, then `rostopic hz /tb_control/wheel_odom` should show
about 50 Hz. Add `-v /var/dockerhome:/home/ohmnidev` to the `docker run` line to see the pushed
scripts at `/home/ohmnidev/sweep-spike`, then:

```
python ros_odom_log.py --out odom.jsonl --seconds 60
python ros_odom_log.py --out square.jsonl --drive-square
```

The first writes ten lines per second with the latest `wheel_odom` sample (x, y, yaw, vx,
wz), `tbcore_status`, and `wheel_encoder`; run the bot shell square from the `dockerenv`
container at the same time to get odometry for that path. The second publishes a 1 m square on
`/tb_cmd_vel` at 0.2 m/s with a stop between legs and 0.5 rad/s left turns (`--side 5` for the
5 m square), and prints the odometry closure error at the end. Compare it with the tape
measurement of the same square. Record both closure errors and the delay from the first
`Twist` to visible motion (a phone video of the wheels with the terminal in frame is enough).

From a laptop with a ROS install on the same LAN the same script runs with
`ROS_MASTER_URI=http://<robot_ip>:11311` and `ROS_IP=<laptop_ip>`; the custom `tbcore_status`
and `wheel_encoder` types then log as `message class not found` unless the `tb_control`
message package is on the laptop, while `wheel_odom` still logs.

## What to record

Fill this in per robot; an empty cell means not measured. An entry becomes hardware evidence
only when the probe output that produced it is attached to #239.

| Item | Robot 1 | Robot 2 | Robot 3 |
|---|---|---|---|
| Model and generation (settings screen) | | | |
| Software version (settings screen) | | | |
| Developer edition (`su` works) | | | |
| `ohmnilabs/ohmnidev` image tag; container `python3 --version` | | | |
| `battery` reply, verbatim | | | |
| `scan_lidar_device` reply; lidar kit present | | | |
| Lidar device path; `telebot_config.json` `serialport` | | | |
| Lidar info (model, firmware, serial) and health | | | |
| Scan rate (rev/s) and points per revolution | | | |
| `apos 0` reply (wheel status) | | | |
| Bot shell latency, median and max (ms); silent commands | | | |
| Bot shell square closure error by tape (m, deg) | | | |
| `manual_move` pulse: speed, ms, distance travelled | | | |
| ROS `wheel_odom` rate; square closure error from odometry (m, deg) | | | |
| ROS square closure error by tape (m, deg) | | | |
| First `Twist` to visible motion (ms) | | | |
| Drive path chosen and why | | | |
| Camera trigger used (call or standalone page) | | | |
| Camera resolution, format code, frame size, fps | | | |
| `ffmpeg` publish: CPU percent, end-to-end latency (ms) | | | |
| `getUserMedia` plus WHIP: works, end-to-end latency (ms) | | | |
| Camera path chosen and why | | | |
| Wi-Fi RSSI, ping loss over 60 s | | | |

## Not yet confirmed on hardware

- The reply text of every bot shell command, and whether commands answer at all (the vendor
  samples only write to the socket).
- The unit and range of `manual_move` speeds (the WebAPI uses -2000..2000 for its own move
  call).
- What `apos <sid>` prints for a wheel, and whether it exposes a position.
- Whether the container may bind `/dev/libcamera_stream` while the telebot app runs, and the
  uid the vendor sample hands the socket to (1047).
- The camera format code the HAL reports (the vendor sample treats frames as PIL mode `L`).
- Whether `lidar_release` is needed when the telebot app never opened the lidar, and whether
  the kit's USB adapter gates motor power on DTR (the probe clears DTR and sends PWM 660 either
  way).
- Whether `tb_control` needs a continuous `Twist` stream or stops on its own when publishing
  stops (the logger publishes at 10 Hz and always ends with zeros).
