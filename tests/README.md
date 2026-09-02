# tests

Capability area: Platform. Milestone: all.

Any engineer may claim a ready task and owns it through review, integration, and evidence.

Cross-cutting tests that do not belong to one package. `test_layout.py` is the contract test for the frozen repository layout (PRD Appendix D, section 8.2): every declared package resolves from this repo, and no undeclared top-level package exists. Package-specific tests live next to their package, for example `relay/tests/`.
