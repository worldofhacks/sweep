# Laptop console

The operator URL is **http://127.0.0.1:5173/**. Use the checkout at
`/Users/quietguy/capystone` on this laptop. Other worktrees preserve development
work; they are not running consoles.

```sh
cd /Users/quietguy/capystone
python3 tools/console.py status
python3 tools/console.py start
```

`start` is idempotent. The host binds only localhost port 5173 and fails if that
port belongs to another process. It never chooses another port. Stop only this
console with `python3 tools/console.py stop`.

The console serves a copied production build, so edits or branch switches do not
change an operator's running interface. After reviewing and validating updates:

```sh
cd console
pnpm install --frozen-lockfile
pnpm test
pnpm lint
cd ..
python3 tools/console.py build
python3 tools/console.py restart
```

`status` and `/console-version.json` identify the running revision, build ID,
source checkout, session and relay origin. These contain no credentials. The
active build is recorded in `.sweep/console/active-build.json`; earlier copied
builds stay inactive under `.sweep/console/releases/`. Process ownership and logs
are in the same private directory.

The host reads `.sweep/console/runtime.json`, a private JSON object with explicit
`SWEEP_RELAY_ORIGIN`, `SWEEP_SESSION_ID`, and `SWEEP_RELAY_TOKEN`. Optional media
settings are `SWEEP_MEDIA_WEBRTC_ORIGIN`, `SWEEP_MEDIA_READ_USERNAME`, and
`SWEEP_MEDIA_READ_PASSWORD`. These are runtime settings, never bundled assets.
Without a complete relay configuration, the console is disconnected with an
empty roster. There is no generated fleet or demo-data URL mode.

Port 8010 is the existing relay; MediaMTX's ports are video infrastructure. These
are dependencies, not extra operator consoles. This launcher does not restart,
reconfigure or send commands to the relay, robots or drones. Old 5174/5175
consoles and redirect servers should remain stopped.

The reconciled console preserves Capture, Flight, Fleet and Swarm gesture
profiles, scrolling recognition feedback, and the earlier consensus-dwell
tuning. Flight remains opt-in and requires neutral release and explicit
confirmation. Hardware availability and capabilities still gate controls.

The current delivery adds modular fleet source changes, per-camera console support,
and documentation. A reviewed console build can be served here without deploying the
updated backend. Before that backend deployment, configure measured
`drive_speed_m_s` and `drive_rotate_speed_deg_s` in `SWEEP_PLANNING_JSON` and
`ground_max_speed_m_s` in `SWEEP_SAFETY_JSON` for the actual robots. These existing
ground-support fields are missing from the current live configuration. Start a new
relay session; the existing live session must not be reused for the update. This
launcher does not perform those steps.

Full nodekit/Ohmni custom telemetry and robot peripheral integration remains paused in
separate work. A visible control or optional telemetry field still requires matching
support in the deployed relay and device. Aerial infrared readings are not integrated
or validated by this delivery, and no additional hardware or second-camera feed is
made connected by a source or console update. See the
[modular fleet delivery boundary](modular-fleet.md#delivery-and-deployment-boundary).
