# Latency artifact -- not producible yet, and here is why

There is no latency artifact in this delivery. `calibration/latency.py` only
formats and bounds-checks *explicitly measured* samples -- it has no synthetic or
estimated mode, and its own docs are explicit: "Do not substitute image decode or
file timestamps: they do not establish glass-to-glass latency." No real timed
samples exist for this venue's DJI-to-MediaMTX pipeline (the aircraft isn't
streaming: battery charging), and inventing plausible-looking numbers would be
exactly the kind of fabricated measurement this task rules out.

This is also a real, load-bearing gate, not paperwork:
`perception.webcam_localization.WebcamLocalization.__init__` refuses to construct
without a latency artifact carrying **at least 20 samples spanning at least 60
measured seconds** with **p95 below 500 ms**, matching the exact camera/pipeline
identity of the calibration. Until real samples exist, `webcam_localization`
cannot run against this stream at all -- see COMMISSIONING.md's blocker list.

## How to measure it once the aircraft is streaming

Follow `perception/WEBCAM_LOCALIZATION.md`'s clock-trial method (section 4):

1. Display `time.monotonic()` on the receiving computer, in view of the camera.
2. Capture at least 20 frames of that clock through the **exact same decode path**
   the live service uses (`perception.webcam_capture`, which pulls RTSP via
   OpenCV/FFmpeg -- not the WHEP/aiortc path this task's DJI evidence used; see
   COMMISSIONING.md's pipeline-mismatch blocker).
3. For each captured frame, `sample_ms = decode_monotonic_s - displayed_clock_value`,
   converted to milliseconds.
4. Record each sample's capture-relative offset in `sample_times_ms` (strictly
   increasing, spanning at least 60000 ms) and the total `duration_ms`.
5. Run:

   ```bash
   uv run python -m calibration latency \
     --samples measured-latency.json \
     --camera-serial <matches the calibration camera_serial> \
     --pipeline <the same pipeline JSON used for calibration, with
                 decoder_path: "opencv-ffmpeg-rtsp" and
                 latency_endpoint: "localization_decode"> \
     --evidence-kind recorded_live \
     --output latency_dji-mini3.yaml
   ```

`measure-latency-template.json` shows the expected shape; it holds no real values
and must not be filled with guessed numbers.
