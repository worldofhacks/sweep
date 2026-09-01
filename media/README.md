# media

Owner: C (Platform). Phase 3.

MediaMTX ingests each drone's stream (RTSP, UDP, or MJPEG) and serves WebRTC and MJPEG/HLS to the console, with recording. Streams are named by drone id (`drone1` to `drone6`). Video runs on the 5 GHz band and control on 2.4 GHz.

Start it with `just media` (or `docker compose up mediamtx`). Config: `mediamtx.yml`.

PRD: sections 5.7, 7.5.
