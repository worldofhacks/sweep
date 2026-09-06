# media

Capability area: Platform. Milestone: M3; one selected live feed is also part of the M2.0 checkpoint.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to stream naming or detection-event transport name one change owner and require cross-review.

MediaMTX ingests each drone's stream (RTSP, UDP, or MJPEG over RTSP) and serves WebRTC (WHEP) and HLS to the console. It does not serve MJPEG: the reduced-fps MJPEG fallback needs a separate transcoder or gateway. [Optional M3 recording](RECORDING.md) runs through a bounded helper and a run-isolated Compose override. Streams are named by device class and unit (`relay/media.py stream_name`): aircraft publish to `drone{unit}` and ground vehicles to `ground{unit}`, where the unit is the device's 1-based position among the configured ids of its class (`relay/README.md`, `SWEEP_DEVICE_CLASSES_JSON`), so the third configured aircraft publishes to `drone3` and the first ground vehicle to `ground1`. The pilot app and the console still derive an aircraft's path from its device id, so configure aircraft ids 1 through N ascending until they derive it from the unit; then `drone{unit}` and `drone{id}` name the same path, and the relay warns at startup when they do not. Video runs on the 5 GHz band and control on 2.4 GHz.

Start it with `just media` (or `docker compose up mediamtx`). Config: `mediamtx.yml`; edits need `docker compose restart mediamtx` because the bind mount does not hot-reload. The image is distroless (no shell), so debug with `docker compose logs mediamtx`.

The DJI Mini 3 pilot app publishes each aircraft's feed over WHIP to `http://<ground-station>:8889/drone{id}/whip`, the id being that node's device id (`adapters/dji_mini3/README.md`, Phase F), which is the same path as `drone{unit}` under the id constraint above. Because MediaMTX runs in a container, it only knows its container address as an ICE candidate; `docker-compose.yml` passes `SWEEP_MEDIA_HOST` (the ground station's LAN IP) into `webrtcAdditionalHosts` so publishers and players on other machines can connect. Export it, or put it in `.env`, and recreate the container (`docker compose up -d mediamtx`) when it changes.

Authentication is fail-closed. The committed configuration has seven publisher accounts (`drone1` through `drone4` and `ground1` through `ground3`), each restricted to its matching path, and one `sweep-reader` account restricted to those seven paths. `docker-compose.yml` maps the passwords by position in `mediamtx.yml`: `MTX_AUTHINTERNALUSERS_0..3` are the aircraft, `_4` the reader, `_5` the API account, and `_6..8` the ground vehicles, so the two files must keep the same account order. Their committed password hash has no known plaintext. A container started without the corresponding `.env` values remains locked; there is no anonymous fallback.

The Android app derives a media-only publisher password from the stored per-node adapter key, so HTTP Basic never exposes the relay/control credential itself. For each deployed node, derive the same lowercase value from the stream name it publishes to (`drone{unit}`, which the app builds as `drone{id}`) and put it in `SWEEP_MEDIA_DRONE{unit}_PASSWORD` (replace the temporary shell variable with that node's adapter key):

```sh
printf %s 'sweep-media-publish-v1:drone1' | openssl dgst -sha256 -hmac "$SWEEP_DRONE1_ADAPTER_TOKEN" -binary | xxd -p -c 256
```

A ground vehicle node derives its password the same way from its own path and adapter key, and the value goes in `SWEEP_MEDIA_GROUND{unit}_PASSWORD`:

```sh
printf %s 'sweep-media-publish-v1:ground1' | openssl dgst -sha256 -hmac "$SWEEP_GROUND1_ADAPTER_TOKEN" -binary | xxd -p -c 256
```

Generate a separate reader password (for example, `openssl rand -hex 32`) and use it as `SWEEP_MEDIA_READ_PASSWORD`; the console uses `SWEEP_MEDIA_READ_USERNAME=sweep-reader`. Also set `SWEEP_MEDIA_WEBRTC_ORIGIN=http://<ground-station>:8889` for the console runtime configuration; the relay serves those three values to the built console at `GET /runtime-config.json` (`console/README.md`, "Live playback").

The control API is on so the relay can project each aircraft's stream state (`relay/README.md`, the `video` field of the state fan-out). `docker-compose.yml` publishes it at `127.0.0.1:9997` only, so it is reachable by processes on the ground station and never from the LAN; a relay running inside the compose network would use `http://mediamtx:9997` instead. The sixth account, `sweep-api`, may only call the API and is locked like the others until `SWEEP_MEDIA_API_PASSWORD` (generate it with `openssl rand -hex 32`) is set; the relay reads the same value together with `SWEEP_MEDIA_API_URL=http://127.0.0.1:9997`. The relay polls `/v3/paths/get/{stream}` for every configured device (`drone{unit}` and `ground{unit}`; the four aircraft paths when no key is configured) at `SWEEP_MEDIA_POLL_INTERVAL_MS` with a `SWEEP_MEDIA_API_TIMEOUT_MS` bound and, when the API stops answering, falls back to each node's own `video_publish_state` after `SWEEP_MEDIA_STALE_AFTER_MS`.

Adding or changing any media credential in `.env` needs the container recreated, not restarted: `docker compose up -d mediamtx`.

Basic authentication protects authorization but plain HTTP does not encrypt media credentials or video. Use this configuration only on the isolated flight-room LAN; use TLS or a trusted VPN before crossing a shared or untrusted network. A WHIP `Location` response is accepted only on the original scheme, host, and port, so the app cannot forward its credential to another origin.

PRD: sections 5.7, 7.5, 8.3.
