"""DJI Mini 3 bridge: one Android node per aircraft and RC-N1 pair (PRD Appendix C).

The Android pilot app lives under ``pilot-app/``; this package holds the Python side
that keeps it honest: the cross-language wire vectors in ``vectors.py``, the
``RemoteBridgeAdapter`` in ``remote.py`` that drives a node over the relay command
wire, and the ``fake_node.py`` stand-in used for wire proofs without hardware.
"""
