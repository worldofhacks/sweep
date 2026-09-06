# BVC velocity review

Reviewed head: `6e2deb1be16981aa3aeca2fe1c91dd483c01796b` (`feat(arbiter): deflect unsafe goto paths with BVC`). The BVC filter correctly changes an unsafe `GOTO` to a safe interim setpoint before the adapter sends it. The head then reports the original plan as completed when the node acknowledges that interim setpoint. For translate and formation plans, no later arrival check or replan distinguishes the requested target from the BVC target. An operator can therefore receive a completed result for a target the aircraft did not reach.

The isolated follow-up branch `fix/bvc-completion-truth` contains `39a306b2a5065b7a25774396aa04c8a3b0bbf949`. It preserves every BVC-rewritten command in `ExecutionResult.deflected_commands`, includes those commands in the wire dictionary, and publishes a terminal lifecycle detail stating that safe interim setpoints were reached while the requested targets remain outstanding. The dispatcher carries the evidence through both direct and resumed completion paths. Route replanning remains a subsequent requested action, so a safety deflection cannot start an unbounded retry loop.

## Evidence

The change is covered by the existing synchronous crossing-path test and a new asynchronous acknowledgement test. Both assert that the terminal result contains the actual BVC setpoint and the outstanding-target detail.

`TMPDIR=/var/tmp/gauntlet /var/tmp/gauntlet/sweep-wiring-combined/.venv/bin/python -m pytest arbiter/test_bvc.py adapters/test_dispatch.py relay/tests/test_autonomy.py -q` passed: 106 tests. Ruff passed for the changed files, `ruff format --check` passed for the changed files, and `git diff --check` passed. The test run emitted 17 pre-existing temporary recording cleanup warnings.

The branch was pushed to `origin/fix/bvc-completion-truth`.
