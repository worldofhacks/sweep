# Search rehearsal

Run `uv sync --locked`, then `pnpm install --frozen-lockfile` and `pnpm build` in
`console`. From the repository root, run:

```sh
node console/scripts/search-browser-smoke.mjs
```

The browser arms and takes off four signed simulated nodes, selects one aircraft,
and completes two searches of the configured atrium. Each search confirms the
server-frozen preview, covers every configured cell, and produces a location from
at least five camera observations. Acknowledging a finding adds no flight command.
The check verifies that the other three aircraft stay in place, then lands the fleet.

Screenshots, relay audit logs, and `evidence.json` are written under
`output/playwright/search-browser-*`. CI runs this check after the fleet and
navigation rehearsals and uploads that directory, including failure screenshots.

For an interactive session, run:

```sh
uv run python -m adapters.sim.demo --search-demo --count 4 --console-dist console/dist
```

Open the URL printed by the process. Use the Search pane after takeoff and select
D-01, whose synthetic camera source is configured for this rehearsal. The demo
paces GOTO and hover acknowledgements so the camera worker can observe each stop.
Frames, camera orientation, detections, and aircraft movement are synthetic.
Physical search accuracy requires the measured deployment evidence described in
issue #89.
