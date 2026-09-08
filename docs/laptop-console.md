# Laptop console

The single operator URL is **http://127.0.0.1:5173/**. Use the source directory and
revision reported by `/console-version.json`; a branch switch does not change the
immutable build already being served. Build and restart from that owned checkout
when publishing a reviewed update.

```sh
python3 tools/console.py status
python3 tools/console.py build
python3 tools/console.py restart
```

The launcher binds only 5173, checks process ownership and never falls back to another
port when 5173 is occupied. Earlier releases remain inactive under `.sweep/console/releases/`.
Store `.sweep/console/runtime.json` with mode 600; it contains `SWEEP_RELAY_ORIGIN`, `SWEEP_SESSION_ID`,
`SWEEP_RELAY_TOKEN`, and the optional media reader origin/user/password. No credentials
enter the static build. No missing bootstrap can create a simulated fleet.

The separate composed relay is loopback port 8010. It is a dependency of the console,
as are MediaMTX's RTSP/WebRTC/API ports. They are not extra operator consoles.
The real-hardware launcher reads only the reviewed private JSON values:

```sh
uv run python -m tools.fleet_relay check --config .sweep/fleet/runtime.json
uv run python -m tools.fleet_relay serve --config .sweep/fleet/runtime.json
```

`check` validates configuration; it does not qualify hardware, create devices, connect to
robots or send commands. `serve` composes the actual intent dispatcher, planner/arbiter,
map services and authenticated device transport. It refuses simulation/shared credentials
and a different port. The normal `.env.example` contains no demonstration roster or guessed
motion policy. Simulator data belongs only in isolated tests.

A process restart requires a new session ID; persisted sessions are replay-only. Update
the private console configuration and each intended device's session/source bindings
explicitly. Stop the previously owned relay after its devices are disconnected; do not
kill a service merely because it occupies a port. Console restart does not restart robots,
DJI controllers, camera publishers or vendor processes.

The earlier [G-01 diagnostics profile](G01_LOCAL_DIAGNOSTICS.md) used `relay.app:app`,
which has no command dispatcher and refuses intents with `downstream_unavailable`.
Use the composed launcher for integrated control, subject to the explicit policy and
hardware limits in [unified fleet control](unified-fleet-control.md).
