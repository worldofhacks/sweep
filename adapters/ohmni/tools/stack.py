"""Retired multi-port launcher; this compatibility entry point changes no processes."""

from __future__ import annotations


def main() -> int:
    print(
        "The legacy Ohmni stack launcher is retired. Use the canonical host launcher "
        "tools/ground_runtime.py and the existing console on port 5173. "
        "No service or robot was started, stopped, or reconfigured."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
