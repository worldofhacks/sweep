# media

Capability area: Platform. Milestone: M3; one selected live feed is also part of the M2.0 checkpoint.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to stream naming or detection-event transport name one change owner and require cross-review.

MediaMTX ingests each drone's stream (RTSP, UDP, or MJPEG over RTSP) and serves WebRTC (WHEP) and HLS to the console. It does not serve MJPEG: the reduced-fps MJPEG fallback needs a separate transcoder or gateway. Recording is switched on in M3 (`record` under `pathDefaults`, plus a mounted `recordings/` volume). Streams are named by drone id with the mapping `f"drone{id}"`, so drone 3 publishes to `drone3`. Video runs on the 5 GHz band and control on 2.4 GHz.

Start it with `just media` (or `docker compose up mediamtx`). Config: `mediamtx.yml`; edits need `docker compose restart mediamtx` because the bind mount does not hot-reload. The image is distroless (no shell), so debug with `docker compose logs mediamtx`.

The DJI Mini 3 pilot app publishes each aircraft's feed over WHIP to `http://<ground-station>:8889/drone{id}/whip` (`adapters/dji_mini3/README.md`, Phase F). Because MediaMTX runs in a container, it only knows its container address as an ICE candidate; `docker-compose.yml` passes `SWEEP_MEDIA_HOST` (the ground station's LAN IP) into `webrtcAdditionalHosts` so publishers and players on other machines can connect. Export it, or put it in `.env`, and recreate the container (`docker compose up -d mediamtx`) when it changes. Publishing needs no credentials: `all_others` accepts any publisher on the LAN until the deferred authentication lands.

PRD: sections 5.7, 7.5, 8.3.
