# media

Current platform scope: one console for an additive fleet of aerial drones and ground robots,
with explicit onboard camera/sensor inventory and real live data only. See the
[modular fleet contract](../docs/modular-fleet.md) and its current implementation/qualification boundaries.
Model-specific milestones and recorded tests below retain their original evidence scope.


Capability area: Platform. Milestone: M3; one selected live feed is also part of the M2.0 checkpoint.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to stream naming or detection-event transport name one change owner and require cross-review.

MediaMTX ingests real aircraft and ground camera streams and serves WebRTC (WHEP) and HLS to the console. It does not serve MJPEG: the reduced-fps MJPEG fallback needs a separate transcoder or gateway. [Optional M3 recording](RECORDING.md) runs through a bounded helper and a run-isolated Compose override. Aircraft retain `drone{id}` paths. Ground streams and additional cameras use the exact `stream` names in the operator's media mapping; a wire ID does not determine a ground unit's camera path.

Start it with `just media` (or `docker compose up mediamtx`). Config: `mediamtx.yml`; edits need `docker compose restart mediamtx` because the bind mount does not hot-reload. The image is distroless (no shell), so debug with `docker compose logs mediamtx`.

The DJI Mini 3 pilot app publishes each aircraft's feed over WHIP to `http://<ground-station>:8889/drone{id}/whip` (`adapters/dji_mini3/README.md`, Phase F). Because MediaMTX runs in a container, it only knows its container address as an ICE candidate; `docker-compose.yml` passes `SWEEP_MEDIA_HOST` (the ground station's LAN IP) into `webrtcAdditionalHosts` so publishers and players on other machines can connect. Export it, or put it in `.env`, and recreate the container (`docker compose up -d mediamtx`) when it changes.

Authentication is fail-closed. The committed configuration preserves eight primary publisher accounts (`drone1` through `drone4`, `ground1` through `ground4`), each restricted to its matching path, and one `sweep-reader` account restricted to those eight paths. Their committed password hash has no known plaintext. A container started without the corresponding `.env` values remains locked; there is no anonymous fallback. The ground variables are `SWEEP_MEDIA_GROUND1_PASSWORD` through `SWEEP_MEDIA_GROUND4_PASSWORD`.

The Android app and Ohmni camera publisher derive a media-only password from the stored per-node adapter key and exact stream name, so HTTP Basic never exposes the relay/control credential itself. The username equals the stream name. For each deployed stream, derive the same lowercase value and put it in the corresponding media password variable. This aircraft example uses that node's adapter key:

```sh
printf %s 'sweep-media-publish-v1:drone1' | openssl dgst -sha256 -hmac "$SWEEP_DRONE1_ADAPTER_TOKEN" -binary | xxd -p -c 256
```

Generate a separate reader password (for example, `openssl rand -hex 32`) and use it as `SWEEP_MEDIA_READ_PASSWORD`; the console uses `SWEEP_MEDIA_READ_USERNAME=sweep-reader`. Also set `SWEEP_MEDIA_WEBRTC_ORIGIN=http://<ground-station>:8889` for the console runtime configuration; the relay serves those three values to the built console at `GET /runtime-config.json` (`console/README.md`, "Live playback").

The control API is on so the relay can project each camera's stream state (`relay/README.md`, the `cameras` field and primary `video` field). `docker-compose.yml` publishes it at `127.0.0.1:9997` only; a relay running inside the Compose network would use `http://mediamtx:9997` instead. The sixth account, `sweep-api`, may only call the API and is locked until `SWEEP_MEDIA_API_PASSWORD` is set; the relay reads the same value together with `SWEEP_MEDIA_API_URL=http://127.0.0.1:9997`. The relay polls each configured `/v3/paths/get/<stream>` at `SWEEP_MEDIA_POLL_INTERVAL_MS` with a `SWEEP_MEDIA_API_TIMEOUT_MS` bound. Configured cameras require successive increasing byte counts in the current connection epoch. When evidence expires after `SWEEP_MEDIA_STALE_AFTER_MS`, node publisher claims cannot restore fresh camera status. Decoded browser frames require a separate playback check.

Adding or changing any media credential in `.env` needs the container recreated, not restarted: `docker compose up -d mediamtx`.

## Two cameras per ground robot and additional units

Use the same explicit mappings in the relay and the media generator. For example,
only after verifying two actual cameras on wire ID 11, its configuration may be:

```dotenv
SWEEP_MEDIA_STREAMS_JSON='{"11":"ground1"}'
SWEEP_MEDIA_CAMERAS_JSON='{"11":[{"camera_id":"head","label":"Head","stream":"ground1"},{"camera_id":"rear","label":"Rear","stream":"ground1-rear"}]}'
```

An explicit camera list replaces the legacy primary mapping for that device; an
empty list stays empty. Every stream must be globally unique. The shared contract
allows 64 device mappings and up to eight cameras per device, so two cameras need
no fleet-specific code change. Configure only actual sources, and keep the wire
IDs consistent with the relay's configured device inventory. Current Ohmni
publishers accept stream names up to 64 characters.

`python -m media.configure` generates a Compose override from these two environment
variables. It reads no passwords and starts nothing. Additional paths receive one
publish-only account each, and the console reader gains access to those exact
paths. Legacy accounts, ports, image pin, API isolation and recording policy are
preserved. The generator uses MediaMTX's indexed environment overrides, including
the array extension supported by the [pinned 1.20.1 loader](https://github.com/bluenviron/mediamtx/blob/v1.20.1/internal/conf/env/env.go).

For each additional path, set `SWEEP_MEDIA_STREAM_<NORMALIZED_STREAM>_PASSWORD` in
the private media environment: uppercase the path and replace `-` with `_`.
`ground1-rear` therefore uses `SWEEP_MEDIA_STREAM_GROUND1_REAR_PASSWORD`. Derive its
password with the same node key and the message
`sweep-media-publish-v1:ground1-rear`; it differs from the head camera's password.
The generator rejects colliding normalized names and reserved account names.
Keep reader/API usernames distinct from all publisher names. Missing or empty
credentials use the locked hash, including for additional cameras.

Prepare and inspect the configuration without starting services:

```sh
mkdir -p .sweep/media
uv run --no-sync --env-file .env python -m media.configure --output .sweep/media/compose.cameras-v1.json
docker compose --env-file .env -f docker-compose.yml -f .sweep/media/compose.cameras-v1.json config --quiet
```

The output contains credential variable references, never password values. It is
created with mode 0600 and refuses to overwrite an existing file. When adding or
removing a camera, generate a new revision filename from the complete mapping.
Use only that revision's override with the base file; stacking old generated
overrides can retain obsolete accounts. When deploying the reviewed configuration:

```sh
docker compose --env-file .env -f docker-compose.yml -f .sweep/media/compose.cameras-v1.json up -d mediamtx
```

Each Ohmni `camera_runner` process owns one V4L source. Two cameras require two
independently configured publishers, each with its verified device node, format,
dimensions and `SWEEP_DEVICE_UNIT`, using the matching node key. The publisher
uses the canonical `drone{unit}` path. Server
permissions do not start these processes, duplicate a feed or establish physical
camera availability. Verify per-camera byte progress and decoded playback after
provisioning. For bounded recording, pass the same generated override through
`media/recording.py --extra-compose-file`; retain its existing ownership and
evidence limits.

Basic authentication protects authorization but plain HTTP does not encrypt media credentials or video. Use this configuration only on the isolated flight-room LAN; use TLS or a trusted VPN before crossing a shared or untrusted network. A WHIP `Location` response is accepted only on the original scheme, host, and port, so the app cannot forward its credential to another origin.

PRD: sections 5.7, 7.5, 8.3.
