"""Summarize source-to-render timestamp pairs from a JSONL capture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from runtime import summarize_frame_latency


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("protocol", choices=("whep", "hls"))
    parser.add_argument("samples", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    samples = [
        json.loads(line)
        for line in args.samples.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = summarize_frame_latency(samples, protocol=args.protocol)
    rendered = json.dumps(report, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
