# Language utterance corpus

`transcript_plan_cases.jsonl` is the source for transcript-to-plan evaluation. Each line is one independently parseable case with an identifier, transcript, relay state, compiler context, expected outcome, category, and `live_demo` marker.

The corpus has 44 cases. Twenty core cases form the live-demo subset. Three additional cases cover explicit multi-ID selection and ordered select-to-flight or select-to-motion plans. `transcript_plan_responses.synthetic.json` is the matching cached provider-response map used for deterministic development runs. The three `estop_pending` cases deliberately expect `unsupported`; voice e-stop remains unapproved until Koby signs off on its input-channel gate. Console and physical RC stops remain the available safety controls.

The expected outcome is either a `plan` with an ordered semantic Intent v1 list, or a non-plan outcome with a typed reason. State and context are part of every case because the compiler grounds output in the authoritative relay projection. Keep case IDs stable because cached provider responses use them as their correlation key.
