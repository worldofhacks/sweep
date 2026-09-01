# media

Owner: C (Platform). Phase 3.

MediaMTX ingests each drone's stream (RTSP, UDP, or MJPEG over RTSP) and serves WebRTC (WHEP) and HLS to the console. It does not serve MJPEG: the reduced-fps MJPEG fallback and the single-feed lens view in Phase 4 need a separate transcoder or gateway. Recording is switched on in Phase 3 (`record` under `pathDefaults`, plus a mounted `recordings/` volume). Streams are named by drone id with the mapping `f"drone{id}"`, so drone 3 publishes to `drone3`. Video runs on the 5 GHz band and control on 2.4 GHz.

Start it with `just media` (or `docker compose up mediamtx`). Config: `mediamtx.yml`; edits need `docker compose restart mediamtx` because the bind mount does not hot-reload. The image is distroless (no shell), so debug with `docker compose logs mediamtx`.

PRD: sections 5.7, 7.5, 8.3.
