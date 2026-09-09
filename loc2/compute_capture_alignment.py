"""Compute T_body_camera for TagLocalizer from a filled-in capture-alignment document.

Run this after the owner has replaced the four MEASURE_METERS_* placeholders in
capture-alignment/webcam_capture_alignment.json with real caliper readings. It
reuses perception.webcam_control_adapter's own pose math so the result matches
exactly what the live adapter computes at the locked gimbal attitude.
"""
import json
import sys
from pathlib import Path

REPO = Path("/home/gauntlet/sweep-agents/opus5-loc-commission")
sys.path.insert(0, str(REPO))

from perception.webcam_control_adapter import CaptureAlignmentDocument, GimbalAttitude

SCRATCH = Path(__file__).parent


def main():
    alignment_path = SCRATCH / "capture-alignment" / "webcam_capture_alignment.json"
    document = json.loads(alignment_path.read_text())
    alignment = CaptureAlignmentDocument.from_document(document)
    attitude = GimbalAttitude(yaw_deg=0.0, pitch_deg=-90.0, roll_deg=0.0)
    matrix = alignment.body_to_camera(attitude)
    print("T_body_camera (paste into config/webcam_localization.json localizer.T_body_camera):")
    print(json.dumps(matrix.tolist(), indent=2))


if __name__ == "__main__":
    main()
