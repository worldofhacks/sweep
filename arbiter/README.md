# arbiter

Capability area: Autonomy. Milestone: M1.

Any engineer may claim a ready task and owns it through review, integration, and evidence. Every arbiter or e-stop change names one change owner and requires cross-review.

Pure Python, no I/O, so every rule is trivially testable. Runs on every intent and every planned command: armed state, network stop state, geofence and ceiling, occupied cells and clearance, spacing minimum after the move, battery reserve for return, drone state validity, confirmation state for risky intents, operator presence, and capture preconditions. Owns two behaviors that ignore all model inputs: network stop (hold, then land if held) and battery return (return to home at reserve, land at critical). The physical RC-N1 and safety operator remain the independent pause, RTH, landing, and takeover path.

Rule: no model in the safety path. Target: every safety rule has a test that tries to break it.

PRD: sections 4.8, 5.5, 7.3, 8.6.
