# Optional camera recording

`docker compose -f docker-compose.yml -f docker-compose.recording.yml up -d mediamtx` enables fMP4 recording for the authenticated `drone1` through `drone4` paths. Ordinary `docker compose up` keeps recording disabled. The override writes host files to `./recordings/` and deletes segments after 24 hours. Create that directory before the first run:

```sh
mkdir -p recordings
docker compose -f docker-compose.yml -f docker-compose.recording.yml up -d mediamtx
docker compose logs -f mediamtx
```

End a recording run by stopping MediaMTX, then archive the completed segments before a later recording startup can delete them:

```sh
docker compose -f docker-compose.yml -f docker-compose.recording.yml stop mediamtx
RUN_ID=2026-09-06-run-01
mkdir -p "flight-evidence/$RUN_ID"
cp -a recordings "flight-evidence/$RUN_ID/recordings"
find "flight-evidence/$RUN_ID/recordings" -type f -name '*.mp4' -print0 | xargs -0 sha256sum > "flight-evidence/$RUN_ID/recording-sha256.txt"
docker compose -f docker-compose.yml -f docker-compose.recording.yml down
```

The paths keep the stream name and MediaMTX recorder time in the directory and filename. fMP4 timing describes arrival and container presentation timestamps at the ground station. It does not establish camera capture time, phone clock alignment, frame correspondence, or a transform to another clock.

Keep the phone ZIP as metadata beside the archived recording hashes. Record its file hash, phone/device identity, camera mode, the stream path, the MediaMTX image digest, and the map, calibration, and clock identities in the rehearsal manifest. The MediaMTX override leaves publisher, reader, playback, and control-API permissions unchanged.
