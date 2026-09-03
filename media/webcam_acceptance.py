"""Initialize the pending laptop-webcam acceptance artifact."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from runtime import webcam_acceptance_template


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".sweep/webcam-acceptance.json"),
    )
    args = parser.parse_args()
    artifact = webcam_acceptance_template(created_at_ms=time.time_ns() // 1_000_000)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
