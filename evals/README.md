# evals

Owner: C (Platform). Phase 1 onward.

Four gold sets (PRD section 4.7):

1. Gesture: recorded webcam sessions with hand-labeled intent timestamps.
2. Language: 200 utterances with gold intent sequences.
3. Simulator scenarios: ten scripted missions with pass/fail assertions on final state and safety log.
4. Hardware acceptance: the scripted mission on real drones, five consecutive passes before any demo.

Sets 1 to 3 run in CI on every merge. Every bug becomes a scenario or a gold-set item before it is fixed.
