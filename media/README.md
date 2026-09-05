# media

The local demo publishes an H.264 canvas source through MediaMTX and renders it through WHEP in the console Live module. Follow [the console demo instructions](CONSOLE_DEMO.md) to run the browser smoke and collect evidence.

`just media` starts the pinned MediaMTX image with `demo.yml`. Compose exposes WebRTC HTTP on loopback port 8889 and WebRTC UDP on loopback port 8189. The console reads `VITE_SWEEP_WHEP_BASE_URL/droneN/whep`. Authentication and recording are disabled for this local demo; physical-camera and aircraft integration remain separate acceptance work.
