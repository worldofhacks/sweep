# media

Media is an attached capability of Sweep’s [modular aerial/ground fleet](../docs/modular-fleet.md).
Current scope includes two onboard cameras on each of at least five ground robots and one
on each aircraft. These are scope requirements, not connected feeds. The operator runtime
plays only actual explicitly configured sources; missing cameras remain unconfigured or
unreported, and one camera’s live status cannot stand in for another.

## Camera inventory

`SWEEP_MEDIA_CAMERAS_JSON` maps a configured device ID to at most eight records
`{camera_id, label, stream}`. Each actual onboard camera gets a distinct ID and a unique
safe stream name. Do not derive a second robot feed from its primary URL or copy its status.
An omitted mapping retains one legacy primary stream; an explicit empty list configures
no cameras. The configured stream and per-camera status drive playback and recording;
parent-device connectivity remains a separate fact. No independent camera device class
is implied by an onboard camera record.

Provision expanded media permissions and recording allowlists from the same explicit
camera inventory. The 64-device software admission ceiling is not a camera throughput or
simultaneous hardware qualification. Measure each source and the combined deployment.

## Generate an explicit deployment bundle

Prepare a private JSON provisioning file with these exact top-level fields:

| Field | Value |
|---|---|
| `cameras` | The explicit device-to-camera mapping used for `SWEEP_MEDIA_CAMERAS_JSON` |
| `publishers` | Records `{user, password, streams}`; each listed stream belongs to exactly one publisher |
| `reader` | One `{user, password}` account allowed to read/play back the configured streams |
| `api` | A separate `{user, password}` account with API access only |
| `webrtc_hosts` | Explicit IP addresses or DNS names reachable by the intended publishers and readers |

Use the actual configured camera inventory and media-only credentials. Camera IDs are
unique within a device; stream names and account names are unique across the bundle.
Passwords contain 32 through 4096 printable bytes; the reserved username `any` is refused.
There are no wildcard or anonymous stream permissions. When a vendor publisher derives
its media password, supply that matching derived value rather than its adapter/control key.

From the repository root, generate the bundle in a new private directory:

```sh
uv run python -m media.configure \
  --provisioning /private/path/media.json \
  --output-dir /private/path/new-bundle
```

This writes `mediamtx.json` and `compose.json` with mode `0600` inside a mode `0700`
directory, refuses to overwrite an existing bundle, and starts no service. Keep the
provisioning input and generated bundle outside source control; they contain credentials.

The generated `compose.json` is standalone. Deployment uses
`docker compose -f /private/path/new-bundle/compose.json` with the intended lifecycle
action; never merge it with the legacy base Compose file and its positional password
overrides. It retains the same MediaMTX service/container identity, so coordinate the
existing service lifecycle rather than starting a parallel server.

For the [recording helper](RECORDING.md), pass
`--base-compose-file /private/path/new-bundle/compose.json` and
`--media-config /private/path/new-bundle/mediamtx.json` together, plus at least one
explicit `--stream NAME`; repeat it for every actual camera to record. The no-flag four-aircraft archive selection is a retained
legacy default, not a fleet ceiling or a source of automatic robot-camera discovery.

## Transport and legacy primary setup

Capability area: Platform. Milestone: M3; one selected live feed is also part of the M2.0 checkpoint.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Changes to stream naming or detection-event transport name one change owner and require cross-review.

MediaMTX ingests explicitly configured aerial and ground camera streams (RTSP, UDP, or MJPEG over RTSP) and serves WebRTC (WHEP) and HLS to the console. It does not serve MJPEG: the reduced-fps MJPEG fallback needs a separate transcoder or gateway. [Optional M3 recording](RECORDING.md) runs through a bounded helper and a run-isolated Compose override. Without an explicit camera mapping, legacy primary streams are named by device class and unit (`relay/media.py stream_name`): aircraft publish to `drone{unit}` and ground vehicles to `ground{unit}`, where the unit is the device's 1-based position among the configured ids of its class (`relay/README.md`, `SWEEP_DEVICE_CLASSES_JSON`), so the third configured aircraft publishes to `drone3` and the first ground vehicle to `ground1`. The DJI pilot app still publishes its primary feed as `drone{id}`. With legacy paths, configure aircraft IDs so ID and unit agree; with explicit camera mappings, match the real publisher’s stream exactly. The console consumes the validated configured path, not an arbitrary adapter URL. Video runs on the 5 GHz band and control on 2.4 GHz.

For the retained legacy bootstrap, `just media` (or `docker compose up mediamtx`) uses `mediamtx.yml`. Expanded explicit deployments use their generated standalone bundle above. Legacy config edits need `docker compose restart mediamtx` because the bind mount does not hot-reload. The image is distroless (no shell), so debug with the matching Compose file's logs command.

The DJI Mini 3 pilot app publishes each aircraft's feed over WHIP to `http://<ground-station>:8889/drone{id}/whip`, the id being that node's device id (`adapters/dji_mini3/README.md`, Phase F), which is the same path as `drone{unit}` under the id constraint above. Because MediaMTX runs in a container, it only knows its container address as an ICE candidate; `docker-compose.yml` passes `SWEEP_MEDIA_HOST` (the ground station's LAN IP) into `webrtcAdditionalHosts` so publishers and players on other machines can connect. Export it, or put it in `.env`, and recreate the container (`docker compose up -d mediamtx`) when it changes.

Authentication is fail-closed. The legacy bootstrap configuration contains `drone1` through `drone4` and `ground1` through `ground4` publisher accounts restricted to their paths, plus a matching reader. That bootstrap is not the modular platform capacity; expanded camera inventory must provision explicit publisher and reader permissions for every configured stream. `docker-compose.yml` maps the passwords by position in `mediamtx.yml`: `MTX_AUTHINTERNALUSERS_0..3` are the aircraft, `_4` the reader, `_5` the API account, and `_6..9` the ground vehicles, so the two files must keep the same account order. Their committed password hash has no known plaintext. A container started without the corresponding `.env` values remains locked; there is no anonymous fallback.

The Android app derives a media-only publisher password from the stored per-node adapter key, so HTTP Basic never exposes the relay/control credential itself. For each deployed node, derive the same lowercase value from the stream name it publishes to (`drone{unit}`, which the app builds as `drone{id}`) and put it in `SWEEP_MEDIA_DRONE{unit}_PASSWORD` (replace the temporary shell variable with that node's adapter key):

```sh
printf %s 'sweep-media-publish-v1:drone1' | openssl dgst -sha256 -hmac "$SWEEP_DRONE1_ADAPTER_TOKEN" -binary | xxd -p -c 256
```

A ground vehicle node derives its password the same way from its own path and adapter key, and the value goes in `SWEEP_MEDIA_GROUND{unit}_PASSWORD`:

```sh
printf %s 'sweep-media-publish-v1:ground1' | openssl dgst -sha256 -hmac "$SWEEP_GROUND1_ADAPTER_TOKEN" -binary | xxd -p -c 256
```

Generate a separate reader password (for example, `openssl rand -hex 32`) and use it as `SWEEP_MEDIA_READ_PASSWORD`; the console uses `SWEEP_MEDIA_READ_USERNAME=sweep-reader`. Also set `SWEEP_MEDIA_WEBRTC_ORIGIN=http://<ground-station>:8889` for the console runtime configuration; the relay serves those three values to the built console at `GET /runtime-config.json` (`console/README.md`, "Live playback").

The control API is on so the relay can project each configured camera’s stream state (`relay/README.md`, the `video` field of the state fan-out). `docker-compose.yml` publishes it at `127.0.0.1:9997` only, so it is reachable by processes on the ground station and never from the LAN; a relay running inside the compose network would use `http://mediamtx:9997` instead. The sixth account, `sweep-api`, may only call the API and is locked like the others until `SWEEP_MEDIA_API_PASSWORD` (generate it with `openssl rand -hex 32`) is set; the relay reads the same value together with `SWEEP_MEDIA_API_URL=http://127.0.0.1:9997`. The relay polls `/v3/paths/get/{stream}` for every configured device (`drone{unit}` and `ground{unit}`; only explicitly configured device paths) at `SWEEP_MEDIA_POLL_INTERVAL_MS` with a `SWEEP_MEDIA_API_TIMEOUT_MS` bound and expires evidence after `SWEEP_MEDIA_STALE_AFTER_MS`. Only a camera named `primary` on its exact legacy path may use that node’s current-epoch `video_publish_state` fallback; additional cameras require their own media evidence. No camera borrows another camera’s live status.

Adding or changing any media credential in `.env` needs the container recreated, not restarted: `docker compose up -d mediamtx`.

Basic authentication protects authorization but plain HTTP does not encrypt media credentials or video. Use this configuration only on the isolated flight-room LAN; use TLS or a trusted VPN before crossing a shared or untrusted network. A WHIP `Location` response is accepted only on the original scheme, host, and port, so the app cannot forward its credential to another origin.

PRD: sections 5.7, 7.5, 8.3.
