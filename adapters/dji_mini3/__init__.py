"""DJI Mini 3 bridge: one Android node per aircraft and RC-N1 pair (PRD Appendix C).

The Android pilot app lives under ``pilot-app/``; this package holds the Python side
that keeps it honest, including cross-language wire vectors, the relay-side remote
adapter, and a fake node for deterministic integration tests.
"""
