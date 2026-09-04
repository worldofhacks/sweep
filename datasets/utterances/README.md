# Language utterance corpus

`transcript_plan_cases.jsonl` is the source for transcript-to-plan evaluation. Each line is one independently parseable case with an identifier, transcript, relay state, compiler context, expected outcome, category, and `live_demo` marker.

The corpus has 50 cases. Twenty cases form the live-demo subset. `transcript_plan_responses.synthetic.json` is the matching cached provider-response map used for deterministic development runs. Capture responses omit `capture_id` because the trusted host mints that identifier; the JSONL expectations retain the resulting semantic identifier for comparison with the exact deterministic host-minted value.

Room references require explicit grounding. “This room,” “the room,” “here,” “that room,” and a named room missing from the authoritative room catalog return `clarify` with `ambiguous_location`. A relative location the system cannot resolve, such as “three doors down,” returns `unsupported`. Missing aircraft selection remains a hard `refuse` result even when the location is also ambiguous.

Translation values are planner-owned steps in the declared `aircraft_relative` frame. Each movement case supplies the configured `step_m` and a current telemetry `heading_deg` for every selected aircraft. Metric language converts through that step size; an unstated distance means one step. The compiler never invents a heading or step size.

Indirect but established flight verbs map to their intents. “Prepare the aircraft for flight” maps to `arm`, and “Launch” maps to `takeoff`; the existing preview and confirmation path remains the safety gate. “Land now” maps to `land` for the current selection, while explicit fleet-wide language maps to `land_all`.

Voice `estop` is reserved for the exact phrase “Emergency stop.” It can produce `estop` only when `estop` appears in the case's `qualified_voice_intents`. “Stop” maps to `hold` when aircraft are selected. “Stop” or “Abort” cancels one authoritative pending preview through `cancel_pending`; without enough state to choose safely, the compiler returns `clarify` with `ambiguous_action`.

When `pano_360` is unavailable and `reconstruct_8` is available, the compiler returns `clarify` with the alternative in `detail`. It never substitutes the capture pattern. A later, explicit request for `reconstruct_8` creates the plan.

The expected outcome is a `plan` with an ordered semantic Intent v1 list, `cancel_pending` with the authoritative pending intent ID, or a typed `clarify`, `unsupported`, or `refuse` result. State and context are part of every case because the compiler grounds output in the authoritative relay projection. Keep case IDs stable because cached provider responses use them as their correlation key.
