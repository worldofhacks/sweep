# arbiter

Owner: B (Autonomy). Phase 1.

Pure Python, no I/O, so every rule is trivially testable. Runs on every intent and every planned command: armed state, e-stop state, geofence and ceiling, spacing minimum after the move, battery reserve for return, drone state validity, confirmation state for risky intents, operator presence. Owns two behaviors that ignore all inputs: e-stop (hover, then land if held) and battery return (return to home at reserve, land at critical).

Rule: no model in the safety path. Target: every safety rule has a test that tries to break it.

PRD: sections 4.8, 5.5, 7.3.
