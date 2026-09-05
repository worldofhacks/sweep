# PR review and merge handoff, 2026-09-05 18:40 UTC

Live GitHub REST snapshot. Main: e043035d8cefe956da9997f195888cb59d629782.
Mergeability is against each PR’s declared base, not necessarily main. Green checks do not clear open findings or dependency holds.

| PR | Head | Base | GitHub merge state | CI | Draft |
|---|---|---|---|---|---|
| #46 | eb0328cefb95abc682b9d1af157d1990a2c51b9e | main | behind | python: success, m14 browser path: success, console: success | False |
| #47 | c90dcc023cfa67ef401141caf77264fc098f0123 | main | dirty | console: success, python: success | False |
| #49 | 74edd67853721389c11b886e2dc41439b8cd1c19 | main | dirty | console: success, python: success | False |
| #68 | 3b7b7cd8fe7a87b0769da920468fb2d9f8e6dbc8 | main | dirty | none | True |
| #102 | 61946e6d671d09a00bf4ce864bc559b685435316 | main | clean | python: success, console: success | False |
| #105 | 9f2267c7fcab2fa68c69787c6717f9ac25a5fe89 | agent/map-survey-bundle | dirty | console: success, python: success | False |
| #106 | 92bd919ec48556c48a029dc1c3f55370d2eda273 | agent/map-survey-bundle | dirty | python: success, console: success | False |
| #107 | 3f6750b58296b601e82acd3914266555c831c5f5 | main | behind | python: success, bridge-jvm: success, console: success | False |
| #108 | d5284b5218b9d86eaab37d84af940abdc81c2549 | main | dirty | python: failure, console: success | False |
| #109 | 6d6723704a64096e386daf108fe6fe1a474a07d5 | feat/issue-37-transcript-compiler | dirty | python: success, console: success | False |
| #110 | 466459e5df3ebef03cebb5f20ee2def58309a1bb | feat/issue-37-transcript-compiler | dirty | python: success, console: success | False |
| #111 | 592df471eb87497de5cdd100b853df6b1c3032c0 | feat/issue-37-transcript-compiler | dirty | python: success, console: success | False |
| #112 | b1d6ced42dc9e9dc11cedb1eb669a11ff781503b | agent/c1-capability-profile | clean | console: success, python: success | False |
| #113 | 6ae47dfea16f6b87642d5f53f944314cda7f06b6 | agent/c1-capability-altitude | clean | python: success, console: success | False |
| #121 | c35727dc72c66a02b7fdd158c17be3a2ff9020b9 | main | behind | python: success, console: success, m14 browser path: success | False |
| #125 | 3237b75d6942efdca2e242632d675c6d6eaba1ae | main | behind | python: success, console: success | False |
| #127 | 78a507b6d916bb3ae44b86a0bbc0dde3df154d26 | feat/issue-43-pilot-app-skeleton | clean | bridge-jvm: success, python: success, console: success | False |
| #132 | b9a8169478fbcc3c800c3bcf45d6b5732ff23295 | agent/offline-tag-localization | clean | python: success, console: success | True |

## Review gates

- #102: clean/current with green checks at 61946e6d. Review 5122039553 now has commit_id 61946e6d, but its body approves 7ab8b98f. No fresh explicit approval on updated head appeared in the read. Check the main-merge delta before approving. Unicode collision follow-up #139 remains deferred.
- #46: eb0328ce is green but behind main; previous findings have replacement code and owner-reported local tests, but no new formal verdict. Current review must verify four-part ACK/E-stop acceptance. Update main and approve resulting SHA before merge.
- #121: c35727dc green but behind, no reviews; includes #46 commits. Integrate after actual #46 merge, validate rollback and concurrency, then review.
- #47/#49: conflicts. #49 has older approval; retain relay changes and use main’s already-ported console. #47 controls belong in ControlModule. Owners retain implementation.
- #109–#113: published heads still have changes requested. #109/#110/#111 conflict with their declared compiler branch; #112/#113 are clean only against stacked branches. Unresolved language, altitude safety, profile and runtime findings remain. These are not established quick final edits. Owning C1 session retains execution.
- #106: old approval on 92bd919e, now conflicts with #102 base. Replay exactly two own commits, then separately adapt digest/provenance/snapshot contracts and obtain current-head review.
- #105: conflicts, no formal review, seven documented preflight repair areas including unsafe pose acceptance. Integrate after #102/#106; do not call it merge-ready based on green old CI.
- #132: clean/green against #105 only, draft. Software review after dependency integration; physical traverse and held-out checkpoints still required.
- #68: old draft conflicts, no checks. Owner has replacement media branch; rebase and publish replacement rather than merge superseded console files.
- #107/#108/#127: bridge work; #107 behind, #108 conflicts and Python CI fails, #127 clean only against #107 branch. No submitted reviews in fetched data. #125 docs is green/behind/unreviewed.

Scope: Koby requests only quick finishing commits and is handing review/merge to a team member. Do not begin broad fixes from this status task. Existing owners report any bounded final changes and clearly identify larger unresolved work.
