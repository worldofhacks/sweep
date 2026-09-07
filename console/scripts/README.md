# Isolated M14 browser regression

`pnpm test:m14-browser` runs the historical button-to-simulator mission in headless
Chromium. It creates temporary audit/cache directories and two unused loopback
ports, excluding the operator console on 5173 and live relay on 8010. Its child
processes receive explicit simulator credentials and opt-in settings, without the
host's relay, device, media, or world-observation configuration. Browser requests
are restricted to those two test origins. Both services and temporary files are
removed when the run finishes or fails.

The production console refuses synthetic device frames. This simulator regression
therefore uses `m14-vite-test.config.mjs`, which requires the harness environment
and cannot build assets. Its narrowly checked source transform disables only that
refusal for the ephemeral test compilation. The production client source,
`vite.config.ts`, and static build remain unchanged; `src/relay/client.test.ts`
independently verifies the production refusal. No URL or production setting can
select this transport.

The mission checks select, arm, takeoff, translate, voice transcription and
confirmation, hold, home, geofence refusal, node watchdog hold/failsafe, network
stop, and landing, including the relay's ordered audit evidence. It requires the
repository Python environment, installed console packages, and Playwright's
Chromium browser. It never installs an app or connects to physical devices.
