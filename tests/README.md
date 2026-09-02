# tests

Owner: C (Platform). Every phase.

Cross-cutting tests that do not belong to one package. `test_layout.py` is the contract test for the frozen repository layout (PRD Appendix D, section 8.2): every declared package resolves from this repo, and no undeclared top-level package exists. Package-specific tests live next to their package, for example `relay/tests/`.
