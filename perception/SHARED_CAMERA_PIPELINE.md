# Shared camera pipeline

`SharedCameraPipeline` is the host-side owner of one `WebcamStream`. Each consumer receives only the newest decoded frame, so an object detector or map builder cannot build a frame backlog that delays localization.

The localization consumer uses `ManagedWebcamLocalizer`, including its pause, release, and fresh-fix revalidation rules. The detector is the existing `LiveDetectionWorker`, sampled at the configured interval. A map builder can be passed as a callback and receives selected `CameraFrame` values. The callback is the integration point for a later mapper.

`WebcamStream` reports host decode-completion time. `CameraFrame.capture_time_monotonic_s` therefore stays `null`; the localizer continues to apply its separately measured decoder-latency estimate. The frame record carries the configured source ID and optional alignment evidence. Gimbal and body transforms remain in their respective calibration evidence.

`WebcamLocalizationService` is the production owner. It creates this pipeline for the
existing host localization process and accepts an optional detector, detection callback,
and map-builder callback. Those consumers share the same decoder with localization.

Run the host entry point with the same pinned localization configuration:

```bash
export SWEEP_LOCALIZATION_RTSP_URL="rtsp://127.0.0.1:8554/drone1"
python -m perception.webcam_localization \
  --config webcam.json --duration 120 --output shared-camera.jsonl
```

Supply `--detector-model`, `--detector-model-sha256`, and `--mission-id` to start the pinned local YOLOX detector. A host composition that needs map keyframes passes its map-builder callback to `WebcamLocalizationService`. The default detector sample interval is 0.2 seconds and the keyframe interval is one second. Camera frame rate depends on the upstream publisher.

Run the offline checks with:

```bash
/var/tmp/gauntlet/sweep-production/system-integration/.venv/bin/python -m pytest \
  perception/test_shared_camera_pipeline.py perception/test_localization_lease.py \
  perception/test_webcam_stream.py perception/test_object_detection.py -q
```
