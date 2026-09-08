# Explicit ground camera publishers

`Camera.from_environment` is passive until the owning node calls `start()`. Without
`SWEEP_GROUND_CAMERAS_JSON`, or with `[]`, no camera publisher is constructed. Setting
`SWEEP_MEDIA_HOST` alone never selects an input. One or two actual inputs can be configured;
a missing second camera is reported separately and never replaced with the first feed.

Each entry requires exactly these fields:

| Field | Meaning |
| --- | --- |
| `camera_id` | Stable lowercase ID, 1–32 characters, matching the relay camera inventory |
| `device` | Explicit `/dev/videoN` or `/dev/v4l/by-id/...` input |
| `stream` | Explicit flat MediaMTX stream name, matching its provisioned path |
| `publisher_user` | Existing publisher account authorized for this stream |
| `input_format` | Actual supported mode: `uyvy422`, `yuyv422`, or `mjpeg` |
| `width`, `height` | Actual even capture dimensions |
| `capture_fps`, `output_fps` | Actual input rate and desired output rate, 1–60; output ≤ input |
| `bitrate_kbps` | Explicit H.264 target bitrate, 64–20000 |

Nonempty configuration also requires `SWEEP_MEDIA_HOST`, `SWEEP_DEVICE_UNIT` (1–64),
and the authenticated device key supplied by the node. `SWEEP_FFMPEG` optionally selects
the executable; its default path is `/data/local/sweep/ffmpeg`. The publisher password
preserves the deployed HMAC-SHA256 derivation using the device key and the exact domain
`sweep-media-publish-v1:{stream}`. Keep these values private. ffmpeg stderr is discarded
because error text can contain its credential-bearing RTSP URL.

The relay's `SWEEP_MEDIA_CAMERAS_JSON` separately associates each device and camera ID
with the same stream. MediaMTX must already authorize the explicit publisher and reader
for each path. This module does not change MediaMTX, derive extra stream permissions,
launch another robot, or change an existing service. Retaining a deployed stream name
retains its password derivation. Adding a stream requires explicit host provisioning.

Feed IDs, device paths and stream names must be distinct. At startup and retries, the
publisher also verifies the input is a character device and prevents two feeds in the
same node from concurrently claiming aliases of the same device. A failed input remains
failed while an independent healthy feed continues. This check does not discover modes
or prove that a device supports the configured mode; ffmpeg failure remains visible.

`device_telemetry.cameras` reports each configured input with `publisher_state`, `fresh`,
`last_frame_age_ms`, `frames`, `output_time_ms`, `evidence` and a bounded diagnostic code.
`evidence: ffmpeg_progress` identifies producer-local encoded/muxed output. A running
process without advancing frame and output-time counters stays `connecting`, then fails
after ten seconds. Output older than two seconds is stale. Failed/stopped feeds are never
fresh. The legacy aggregate state is `publishing` only while every configured feed is fresh;
the individual rows preserve partial success. The relay independently checks MediaMTX;
local output is not proof of network delivery or browser playback.

Publication provides no camera capture, zoom or tilt control capability. Those require
separate supported-operation contracts and actual hardware implementation.

The host's current inventory identified a See3CAM_CU135 at `/dev/video0` and an HD USB
Camera at `/dev/video1` on one powered robot. That observation alone does not establish
the second camera's capture format, dimensions, frame rate, stream permissions or current
freshness. Discover and explicitly configure those values before starting its publisher;
there is no inferred second-camera default.
