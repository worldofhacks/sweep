# Console video demo

The smoke run starts MediaMTX and Vite, publishes an animated H.264 canvas through WHIP, and opens the actual console's Live module in Chromium. It checks WHEP negotiation, advancing decoded video, an accepted network-stop fixture request, session teardown on navigation, and playback after reopening Live.

Run from the repository root with the official MediaMTX 1.20.1 binary and the console's existing Playwright installation:

```bash
mkdir -p .sweep/media-demo
media_image_container=$(docker create bluenviron/mediamtx:1.20.1)
docker cp "$media_image_container:/mediamtx" .sweep/media-demo/mediamtx
docker rm "$media_image_container"
MEDIAMTX_BINARY="$PWD/.sweep/media-demo/mediamtx" node media/console_demo_smoke.mjs
```

The script binds MediaMTX HTTP to `127.0.0.1:18889`, its WebRTC UDP listener to `127.0.0.1:18189`, and Vite to `127.0.0.1:14175`. It sets `VITE_SWEEP_WHEP_BASE_URL` for Vite and stops its processes when the run ends. Set `SWEEP_MEDIA_DEMO_ARTIFACTS` to choose an output directory; otherwise `.sweep/media-demo` contains JSONL evidence, server logs, and a screenshot of the rendered console.

The fleet and control acknowledgements use the console's `control` fixture. Video travels through real browser peer connections and MediaMTX. Authentication, recording, physical cameras, and aircraft control remain outside this demo.

`media/demo.yml` uses port 8889 and UDP 8189 for Docker, with loopback host mappings supplied by Compose. The standalone smoke overrides those listeners to the isolated loopback ports above. The stream URL contract is `VITE_SWEEP_WHEP_BASE_URL/droneN/whep`.

Protocol and configuration references: [MediaMTX browser publishing](https://mediamtx.org/docs/publish/web-browsers), [MediaMTX WebRTC readers](https://mediamtx.org/docs/read/webrtc), and the [pinned server configuration](https://github.com/bluenviron/mediamtx/blob/v1.20.1/mediamtx.yml).
