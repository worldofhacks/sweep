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

This consolidation publishes console changes only. The separate relay, robot
runtime and Android deployment work remains paused; a visible control still
requires support advertised by the actual connected backend and device.
