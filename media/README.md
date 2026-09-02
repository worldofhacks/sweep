# media

Capability area: Platform. Milestone: M3; one selected live feed is also part of the M2.0 checkpoint.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to stream naming or detection-event transport name one change owner and require cross-review.

MediaMTX ingests each drone's stream (RTSP, UDP, or MJPEG over RTSP) and serves WebRTC (WHEP) and HLS to the console. It does not serve MJPEG: the reduced-fps MJPEG fallback needs a separate transcoder or gateway. Recording is switched on in M3 (`record` under `pathDefaults`, plus a mounted `recordings/` volume). Streams are named by drone id with the mapping `f"drone{id}"`, so drone 3 publishes to `drone3`. Video runs on the 5 GHz band and control on 2.4 GHz.

Start it with `just media` (or `docker compose up mediamtx`). Config: `mediamtx.yml`; edits need `docker compose restart mediamtx` because the bind mount does not hot-reload. The image is distroless (no shell), so debug with `docker compose logs mediamtx`.

PRD: sections 5.7, 7.5, 8.3.
