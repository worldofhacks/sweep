# Prior art for intent mapping and grounded execution

## Summary

Sweep's existing boundary is stricter than most embodied-agent systems reviewed here. SayCan limits a language model to a fixed skill list and grounds each choice with learned affordances. Code as Policies and VoxPoser run model-generated code through bounded robot APIs. RT-2 predicts robot actions directly. Eureka searches over model-generated reward code in simulation. These systems contribute useful grounding and evaluation techniques, but none provides Sweep's complete chain of a frozen intent contract, deterministic validation, operator preview, deterministic planning, command-level arbitration, and adapter isolation.

The closest runtime precedents are SELP, SafeGate, and a 2026 industrial neuro-symbolic planning system. They place a machine-checkable feasibility or safety decision after the model proposal. SafeGate and the industrial system also recheck plans as state changes. ASIMOV-Agentic and EMBODYGUARD provide benchmark taxonomies for evaluating the same boundaries, although ASIMOV uses model judging for some tasks. Sweep should preserve its current architecture and borrow the runtime systems' state-freshness checks plus the benchmarks' failure categories and artifact shapes.

For webcam gestures, the reusable prior art is temporal consensus, hysteresis, one-shot emission, release-to-rearm, explicit input states, and event-level evaluation. MediaPipe's default 0.5 hand detection, presence, and tracking thresholds are library tracking controls. They do not justify accepting a hardware command. No source establishes a universal classifier score, dwell, stillness, or cooldown threshold. Sweep's current score of 0.8 and dwell of 600 ms, with 400 ms for confirm and cancel, are reasonable initial values because they are already in the PRD. They should later be selected from timestamped replay against the false-activation, recall, duplicate-emission, and latency targets.

For the 200-utterance language set, the most directly aligned benchmark reviewed here is Google's 2026 ASIMOV-Agentic release. It separately tests safe execution, ambiguity, infeasible capabilities, and runtime safety events. EMBODYGUARD contributes context-dependent hazards and fine-grained failure labels. DialFRED and TEACh contribute clarification language. ALFRED contributes paraphrase and multi-step variation. DESPITE and RoboJailBench contribute deterministic safety and paired adversarial cases. The external datasets should supply case shapes and linguistic variation; Sweep's gold actions and verdicts should be rewritten into Intent v1 and checked by the real validator, planner, arbiter, simulator, and capability oracle.

The main security gaps are implementation details rather than missing components. Strict JSON does not establish that a drone exists, a reference is unique, a capability is available, or the plan remains safe after preview. Confirmation should authorize a digest of one plan and one state snapshot. Every intent should be revalidated immediately before emission. Compiler context should be built from an allowlisted typed state projection, with no raw perception text, device strings, logs, or adapter errors. Provider refusals, malformed output, ambiguity, unsupported capability, stale state, safety refusal, and dispatch failure need distinct typed outcomes that all fail closed.

## Implementation guidance

These changes fit the PRD and the scaffolding draft without adding another planning layer:

1. Implement the model output as a tagged result with mutually exclusive `plan`, `clarify`, `unsupported`, and `refuse` outcomes. Provider refusal, timeout, truncation, and malformed output remain separate transport or parsing failures.
2. Bind a preview to a plan digest, state version, capability profile version, expiry, and operator decision. Revalidate before every intent emission and command dispatch.
3. Record every compiler boundary in the eval artifact: raw provider result metadata, parsed result, schema verdict, resolver verdict, capability verdict, planned commands, arbiter verdict, operator decision, and execution result.
4. After the webcam conformance runner (M1.3) restores and tests the prototype behavior unchanged, evaluate a deterministic acceptance state machine for the later gesture-hardening work: `idle -> candidate -> accepted -> wait_for_release -> idle`. Use monotonic elapsed time and test enter/leave hysteresis plus a neutral or no-hand release condition through replay before changing production behavior.
5. Build the 200 language cases across successful plans, clarification, unsupported capabilities, safety refusals, changed-state invalidation, injection attempts, and benign hard negatives. Preserve the existing exact-match threshold and add diagnostic execution-validity and failure-reason metrics through the capability execution oracle and the capability-aware compiler eval.

## 1. Embodied LLM planners

### SayCan

Sources: [paper](https://arxiv.org/abs/2204.01691), [project and results](https://say-can.github.io/), [Google Research explanation](https://research.google/blog/towards-helpful-robots-grounding-language-in-robotic-affordances/)

SayCan asks a language model to score a finite list of language-described skills. A learned value function scores whether each skill can succeed in the current state. The system combines usefulness and affordance, executes the selected pretrained skill, then repeats. The model chooses among known skills rather than inventing motor commands.

Sweep can reuse the separation between semantic relevance and embodiment feasibility. Intent v1 is the finite skill list, and the capability execution oracle is the stronger deterministic feasibility check. The M4 language report should distinguish wrong intent, unsupported capability, unsafe state, and execution failure. Changed-state cases should include battery reserve, positioning loss, link loss, a new selection, and a drone that changed flight state after preview.

SayCan's affordance score is learned and probabilistic. It is useful for ranking candidates, but it is not a safety proof. SayCan also executes one skill at a time without Sweep's whole-plan preview. Sweep's validator and arbiter should remain authoritative.

### Code as Policies

Sources: [paper](https://arxiv.org/abs/2209.07753), [project and generated programs](https://code-as-policies.github.io/), [Google Research explanation](https://research.google/blog/robots-that-write-their-own-code/)

Code as Policies prompts a model to generate Python that composes perception APIs, robot primitives, library calls, loops, and generated helpers. It demonstrates strong spatial and compositional reasoning, but the authors also identify feasibility and unintended-composition limits.

Sweep can reuse its evaluation categories: unseen compositions of known actions, synonyms, spatial paraphrases, and plans longer than the prompt examples. Prompts should describe a small named surface, which for Sweep is the Intent v1 plan schema.

Generated Python and direct access to perception or robot APIs conflict with Sweep's frozen boundary. Sandboxing code would reduce arbitrary-code risk but would not establish physical safety. Appendix C should never be exposed to the compiler.

### VoxPoser

Sources: [paper](https://arxiv.org/abs/2307.05973), [project](https://voxposer.github.io/), [official repository](https://github.com/huangwl18/VoxPoser)

VoxPoser uses model-generated code to compose 3D affordance and constraint maps. A conventional model-based planner turns those maps into trajectories. The generated map program can be rerun against new perception, allowing closed-loop replanning without a new language-model call.

Sweep can borrow the separation of language interpretation from current-state grounding. `resolve_selection` and `resolve_location` should turn phrases into typed values, while confirmation and dispatch recheck the current state. Eval results should distinguish reference resolution, capability feasibility, motion planning, and safety arbitration.

VoxPoser's open-vocabulary 3D value maps and generated code are unnecessary for Sweep's frozen mission. They should not replace the current resolvers or deterministic planner.

### RT-2

Sources: [paper](https://arxiv.org/abs/2307.15818), [Google DeepMind explanation](https://deepmind.google/blog/rt-2-new-model-translates-vision-and-language-into-action/)

RT-2 represents end-effector actions as text-like tokens and predicts the next robot action directly from images and language. Its contribution is semantic and visual transfer from web-scale training into action selection.

Sweep can reuse its evaluation idea of holding out unseen combinations, synonyms, and semantic attributes. RT-2's direct action prediction bypasses Intent v1, deterministic planning, validation, and arbitration, so it is unsuitable for the plan compiler.

### Eureka

Sources: [paper](https://arxiv.org/abs/2310.12931), [project](https://eureka-research.github.io/), [official repository](https://github.com/eureka-research/Eureka)

Eureka generates reward functions, trains policies in parallel Isaac Gym simulations, returns training statistics to the model, and iterates. It is an offline reward-design and skill-learning system rather than an online command compiler.

Sweep can reuse its generate, simulate, score, and revise loop while developing prompts or choosing models. Each compiler candidate should run through the real schema, resolver, validator, planner, arbiter, capability oracle, and simulator. Typed aggregate failures are better prompt-development feedback than a single exact-match score.

Model-generated reward code and reinforcement learning do not belong in Sweep's runtime path.

### ProgPrompt, LLM+P, and Inner Monologue

Sources: [ProgPrompt paper](https://arxiv.org/abs/2209.11302), [LLM+P paper and code link](https://arxiv.org/abs/2304.11477), [Inner Monologue paper](https://arxiv.org/abs/2207.05608)

ProgPrompt supplies program-like descriptions of available actions and objects so a model emits executable task plans. LLM+P has the model translate a task into PDDL, lets a classical planner solve it, then translates the solution back. Inner Monologue feeds scene, success, and human feedback back into a language planner after each step.

Sweep can reuse explicit action and object inventories, deterministic plan validation, and typed post-step feedback. LLM+P also supports the scaffolding draft's decision to score model proposals by running deterministic planning rather than asking a model to grade itself.

Full PDDL translation and an iterative model loop add little value for fifteen frozen intents and a six-drone mission. Raw environment feedback also creates a prompt-injection surface. Sweep should return typed outcome codes to the compiler or UI and keep free-form device output outside model context.

### AutoRT

Sources: [paper](https://arxiv.org/abs/2401.12963), [Google DeepMind safety description](https://deepmind.google/blog/shaping-the-future-of-advanced-robotics/)

AutoRT uses a vision-language model for scene description, a language model for task proposals, and a language-model filter for feasibility and semantic safety. Its physical deployment also uses classical joint-force stops, human supervision, and a physical deactivation switch. Google states that prompting and self-critique do not guarantee safety.

Sweep should borrow the layered test strategy. Semantic refusal belongs in the compiler eval, while geofence, altitude, spacing, battery, operator presence, link loss, and e-stop remain deterministic. AutoRT's model-based safety filter is defense in depth only and should not replace Sweep's arbiter.

### SELP

Sources: [paper](https://arxiv.org/abs/2409.19471), [project](https://lt-asset.github.io/selp/), [official repository](https://github.com/lt-asset/selp)

SELP samples natural-language-to-LTL translations, groups equivalent formulas, selects by vote, converts the formula to an automaton, and constrains candidate actions during decoding. Its evaluation includes drone navigation and manipulation.

Sweep can reuse its trace artifacts and temporal test categories. Store raw output, parsed plan, validation result, planned commands, arbiter result, and final execution score. Add cases for ordering, exclusion, completion before return, and paraphrase consistency.

LTL voting and automaton-constrained decoding add latency and complexity that the current intent set does not require. The existing validator and planner can express the capstone rules directly.

### SafeGate and task safety contracts

Source: [2026 preprint](https://arxiv.org/abs/2604.05427)

SafeGate binds extracted hazard properties to a human-curated hazard library and applies a deterministic authorize, defer, or reject gate. Authorized tasks receive contracts with invariants, guards that encode preconditions, and abort conditions. The system uses Z3 for static checks and monitors contracts at runtime.

Sweep can reuse the outcome and contract vocabulary. Ambiguity or missing state should defer for clarification. Unsupported or unsafe operations should refuse. Geofence, spacing, altitude, battery, link, and positioning rules can be recorded as preconditions or runtime abort reasons while remaining in the current arbiter. A plan that was safe at preview can still be stopped after state changes.

SafeGate uses a model in hazard extraction. Sweep's hazards and intent set are closed enough to encode directly, preserving the rule that no model participates in safety. Z3 is unnecessary unless later requirements produce constraints that are hard to express and test in ordinary code.

### Industrial neuro-symbolic planning with a digital twin

Source: [2026 preprint](https://arxiv.org/abs/2606.08214)

This industrial system gives language interpretation and contextual reasoning to neural agents while a non-model Inspector checks geometry and assembly constraints. Tool inputs and outputs are typed JSON. The operator previews a verified plan and trajectory in a digital twin. Operator edits are reverified, and execution failures return as structured events before another attempt.

Sweep can borrow four implementation rules: validate even after strict schema generation, revalidate after any operator edit, bind preview to verified state, and return typed failures from every boundary. A useful minimum is `schema_invalid`, `clarification_required`, `unsupported_capability`, `safety_refusal`, `planning_failure`, and `dispatch_failure`.

Its multi-agent orchestration and full digital-twin stack are larger than Sweep needs. The simulator preview and existing console are sufficient for the capstone.

### Directly reusable open-source analogues

[ros2_lingua](https://github.com/purahan/ros2_lingua) is an early Apache-2.0 ROS 2 project with a capability registry, structured action plans, backward prerequisite chaining, and a dispatcher. Its useful pattern is a ROS-independent schema and registry core that can be tested without hardware. Its README labels the project early development, and dispatcher parameter validation was still on its roadmap when reviewed. Sweep should treat it as implementation inspiration rather than a safety dependency.

[URML](https://github.com/URML-MARS/URML) is a 2026 Apache-2.0 intent language and validator with robot manifests and safety envelopes. Its static validation and conformance-suite organization resemble Sweep's conformance suite, capability oracle, and capability-aware eval. The scaffolding draft explicitly rejects a universal vehicle language, so Sweep should borrow test organization or reason-code ideas only.

[LaMMA-P](https://arxiv.org/abs/2409.20560) combines language decomposition, PDDL validation, and capability-aware allocation for heterogeneous robots. Its vague and capability-limited cases can inform B2. Its general PDDL domain would duplicate Sweep's small deterministic planner.

## 2. Gesture to intent

### MediaPipe Tasks

Sources: [Gesture Recognizer guide](https://developers.google.com/edge/mediapipe/solutions/vision/gesture_recognizer), [Web guide](https://developers.google.com/edge/mediapipe/solutions/vision/gesture_recognizer/web_js), [GestureRecognizer options](https://ai.google.dev/edge/api/mediapipe/python/mp/tasks/vision/GestureRecognizerOptions), [current web worker sample](https://github.com/google-ai-edge/mediapipe-samples-web/blob/main/src/workers/gesture-recognizer.worker.ts)

MediaPipe exposes separate controls for hand detection, hand presence, tracking, and gesture-classifier score. Detection, presence, and tracking default to 0.5. The canned classifier has seven named gestures plus `None`; Sweep's larger semantic vocabulary therefore depends on the recovered prototype mappings or a custom classifier.

The browser's `recognize()` and `recognizeForVideo()` calls are synchronous. Google's current sample moves recognition into a worker. Sweep can use the same boundary so inference cannot stall dwell feedback, confirmation UI, or keyboard e-stop handling. MediaPipe live-stream implementations may drop frames to preserve latency, so dwell should use monotonic timestamps rather than a number of frames.

The library defaults should remain distinct from Sweep's command-acceptance threshold. A visible, tracked hand establishes the prerequisite for classification and provides no evidence of operator intent.

### Tello MediaPipe gesture control

Sources: [kinivi/tello-gesture-control repository](https://github.com/kinivi/tello-gesture-control), [`GestureBuffer` implementation](https://github.com/kinivi/tello-gesture-control/blob/main/gestures/gesture_recognition.py#L386-L399), [Google developer write-up](https://developers.googleblog.com/drone-control-via-gestures-using-mediapipe-hands/)

This Apache-2.0 project is the closest open-source MediaPipe-to-drone mapper found. It buffers ten classifications, accepts a label when it occupies at least nine slots, then clears the buffer. The controller maps accepted labels directly to Tello movement, stop, or land behavior.

Sweep can reuse temporal consensus and clearing the acceptance state after emission. It should replace frame-count timing with elapsed time, retain score and stillness checks, and require neutral or no-hand release before the same held pose can emit again. The direct velocity and land calls are inapplicable because they bypass Sweep's intent, planner, and arbiter chain.

[Kazuhito00's MediaPipe sample](https://github.com/Kazuhito00/hand-gesture-recognition-using-mediapipe) is useful for data collection. It records normalized landmark vectors for static gestures, keeps a short fingertip history for dynamic gestures, and smooths labels by recent mode. Its classifier and thresholds are demonstration assets rather than drone-safety evidence.

### Dwell, hysteresis, clutching, and tracking loss

[Gesture commands for high-level UAV behavior](https://link.springer.com/article/10.1007/s42452-021-04583-8) uses a physical button to indicate when the operator is gesturing, classifies overlapping windows, and maps gestures to high-level search, return, confirm, and reject behaviors. The reusable concept is a clutch that narrows the period when ordinary motion can become a command. Sweep already has armed state, confirmation, operator presence, and a visible input mode that can serve this role.

A [2026 edge HRI system](https://www.mdpi.com/2073-431X/15/4/241) uses dwell plus different enter and leave thresholds before a finite-state robot controller. Open palm acts as a motion clutch, low tracking confidence inhibits movement, and sustained tracking loss requires deliberate re-enablement. The paper measures event delay and false activations per minute. Its reported probability and dwell values belong to its wave classifier and cobot setup and should not be copied into Sweep.

[Single-Handed Gesture Recognition for Drone Motion Control](https://www.mdpi.com/2076-3417/14/22/10230) contributes an explicit neutral class and examples gathered during transitions. Its 81 direct flight-control combinations conflict with Sweep's high-level intent vocabulary. [Drone Control in AR](https://www.mdpi.com/2504-446X/6/2/43) contributes a calibrated neutral zone for involuntary movement, which is relevant to stillness and future continuous directional inputs.

Several smaller repositories publish example dwell and cooldown constants. Their values range from a few hundred milliseconds to two seconds and were tuned for desktop or media control. They support the state-machine pattern but provide no drone-safety evidence. They should not set Sweep's constants.

### Stillness

MediaPipe returns 21 normalized image landmarks and, where supported, world landmarks. A replayable stillness measure can use the wrist and palm MCP joints, calculate displacement per elapsed second, normalize image displacement by palm width, and aggregate across the dwell interval. A median or high percentile prevents one noisy landmark from dominating.

This stillness measure is an implementation inference from MediaPipe's coordinate contract. Google provides no vendor threshold for it. If landmark jitter is material, the [One Euro Filter](https://hal.science/hal-00670496) is a suitable low-latency smoother. Its parameters must be included in the threshold grid because smoothing changes both false activations and onset latency.

### Threshold selection for Sweep

No reviewed source provides a universal operating point. Preserve the PRD values during the M1 wiring work. Once the prototype artifact and recordings are available, replay the same timestamped candidate stream over a grid of score, dwell, stillness, enter/leave hysteresis, disappearance grace, and rearm settings.

Each run should report:

- event precision and recall against labeled intent windows;
- false activations per minute during ordinary and fast random hand motion;
- duplicate emissions per intended hold;
- p50 and p95 time from labeled gesture onset to intent emission;
- failures per gesture and per transition;
- rejection reasons, including low score, motion, tracking loss, label change, and timeout.

Include hand entry and exit, occlusion, multiple people, label-boundary wobble, intended transitions, and at least the PRD's five minutes of fast random movement. Frame-level classifier accuracy remains useful for diagnosis, but accepted events are the safety-facing unit.

## 3. Language datasets and benchmarks

### Best candidates

| Source | What it contains | Reuse for Sweep | Limits |
|---|---|---|---|
| [ASIMOV-Agentic](https://huggingface.co/datasets/google/asimov_agentic), with [technical report](https://storage.googleapis.com/deepmind-media/gemini-robotics/Gemini-Robotics-2-Safety.pdf) | 2026 safety harness and CC BY 4.0 data for physical constraints, uncertainty resolution, infeasible capabilities, safety tool calls, and runtime monitoring | Adapt ambiguity classes, capability pairs, safe controls, and changed-state cases. Its Parquet plus tool-call harness is a concrete packaging reference. | Mostly manipulation and multimodal scenes. Some verdicts use model judging. Rewrite cases into authoritative Sweep fixtures and deterministic verdicts. Access requires accepting the dataset conditions. |
| [EMBODYGUARD and SAFEL](https://aclanthology.org/2025.emnlp-main.1305/), with [code and data](https://github.com/Yonsei-MIR/EAI-safety) | 942 PDDL-grounded scenarios covering overtly malicious and context-dependent hazardous instructions | Separate refusal, goal interpretation, transition modeling, and action sequencing failures. Translate hazards into battery, spacing, geofence, positioning, arm, mode, and confirmation states. | Household action vocabulary. Reuse construction and labels, not action strings. |
| [SafeAgentBench](https://safeagentbench.github.io/) | 750 embodied tasks, including 450 hazardous tasks across ten categories, with safe, unsafe, abstract, and long-horizon groups | Build paired safe and unsafe cases. Use long-horizon cases where a safety action or condition must precede a later step. | AI2-THOR household domain and model-based semantic grading. Sweep can use its deterministic path. |
| [DialFRED](https://github.com/xfgao/DialFRED) | 53,000 human-annotated clarification question and answer pairs over ALFRED | Reuse question forms for identity, location, direction, missing target, and vague quantities. | No refusal or hardware safety focus. Use for language variation only. |
| [TEACh](https://github.com/alexa/teach) | Human-human dialogue aligned to structured simulator actions | Build multi-turn cases where later operator input resolves an earlier reference. | Household tasks and success focus. |
| [ALFRED](https://github.com/askforalfred/alfred), with [CVPR paper](https://openaccess.thecvf.com/content_CVPR_2020/html/Shridhar_ALFRED_A_Benchmark_for_Interpreting_Grounded_Instructions_for_Everyday_Tasks_CVPR_2020_paper.html) | 25,743 free-form directives for 8,055 replayable expert demonstrations, with high-level and step-level instructions | Source paraphrase structure, high-level versus explicit forms, and multi-step composition. | Success-only household data. It should fill benign cases only. |
| [DESPITE](https://arxiv.org/abs/2604.18463) | 12,279 physical and normative safety tasks with deterministic validation | Reuse its feasible-safe, feasible-unsafe, and infeasible labels. Add semantic execution validity beside exact match. | General embodied tasks, not operator drone language. Rewrite and review all cases. |
| [RoboJailBench](https://arxiv.org/abs/2605.19328) | An 18-category security taxonomy and paired benign/adversarial intent construction over embodied datasets | Use paired controls to measure both attack resistance and benign utility. | VLM and jailbreak focus. Sweep's text compiler needs a smaller domain-specific subset. |
| [PlanBench](https://arxiv.org/abs/2206.10498) and [ACTIONREASONINGBENCH](https://arxiv.org/abs/2406.04046) | Automatically generated planning, action-executability, effects, state tracking, and hallucination tasks with planner validation | Reuse deterministic generation and failure categories for state/action reasoning tests. | Abstract PDDL domains and templated language are poor sources of responder phrasing. |

### Suggested 200-case composition

| Cases | Expected result | Main source pattern |
|---:|---|---|
| 60 | Exact Intent v1 sequence | ALFRED and TEACh compositional language |
| 35 | Clarification | ASIMOV-Agentic and DialFRED ambiguity classes |
| 25 | Unsupported capability | ASIMOV-Agentic feasibility cases |
| 30 | Safety refusal | EMBODYGUARD and SafeAgentBench paired hazards |
| 25 | Initially valid, refused after state change | ASIMOV runtime events and SafeAgentBench long-horizon conditions |
| 15 | Prompt injection or authority spoofing | AgentDojo and robot sensory-injection studies |
| 10 | Benign hard negatives containing security or safety language | Paired controls for injection and refusal tests |

Every case should include the utterance, authoritative state fixture, capability profile, expected plan or typed non-plan outcome, expected resolver and safety verdicts, and a reason code. The responder review remains important because the source datasets largely describe homes and manipulation rather than noisy indoor swarm operations.

Keep these metrics separate:

- exact plan match;
- schema-valid output rate;
- clarification recall and over-clarification rate on clear requests;
- unsupported-capability recall;
- safety-refusal recall;
- capability-valid execution rate through the capability oracle;
- unsafe dispatch count;
- latency, token use, and cost.

DESPITE accepts alternative plans that are feasible and safe, which exposes a limitation of exact match. Sweep's 85 percent exact-match target should remain for the frozen mission. The capability-aware eval's deterministic execution verdict should be reported beside it so a harmless alternative is distinguishable from a wrong or unsafe plan.

## 4. Structured output and the safety boundary

### Schema guarantees and their limits

Sources: [OpenAI Structured Outputs documentation](https://openai.com/index/introducing-structured-outputs-in-the-api/), [Berkeley Function Calling Leaderboard](https://gorilla.cs.berkeley.edu/leaderboard)

Strict structured output constrains shape. It does not establish that field values are correct, referenced entities exist, the requested capability is available, or execution is safe. Provider refusal and truncated generation also need paths outside the success schema. BFCL separately tests missing functions, missing parameters, irrelevant calls, and hallucinated functions, which are the semantic cases Sweep's compiler eval needs.

For Sweep:

- use one schema-constrained plan result and disable parallel tool calls;
- reject unknown properties recursively with `additionalProperties: false` or the equivalent;
- bound plan length, string length, array length, integer IDs, enum values, and numeric deltas;
- reject non-finite values, duplicates, nonexistent IDs, empty motion selections, and stale state;
- treat any parse, provider, retry, or validation failure as zero emitted intents;
- use function calling for serialization; deterministic code performs authorization.

### Capability hallucination

[ros2_lingua](https://github.com/purahan/ros2_lingua) validates proposed actions against a registered capability set before dispatch. [Precise Robot Command Understanding Using Grammar-Constrained LLMs](https://arxiv.org/abs/2604.04233) canonicalizes model output into known action frames, validates it with a grammar parser, and retries invalid generations. [Decode-Time Grammars](https://arxiv.org/abs/2607.18357) makes the broader point that grammar-valid output can still contain references absent from the current runtime environment.

Sweep already has the right answer in the conformance suite, capability oracle, and capability-aware eval: grammar limits names, the source registry limits producers, resolvers validate references, the oracle validates adapter capability, and the arbiter validates current safety. A model-facing capability summary may improve proposals, but only the registered profile grants authority.

Automatic retry is suitable for malformed syntax before preview. Semantic ambiguity should return clarification rather than letting repeated model calls silently choose a meaning.

### Ambiguous references

[OK-Robot](https://arxiv.org/abs/2401.12202) reports language-to-object ambiguity as a concrete source of failure and proposes user confirmation after retrieval. ASIMOV-Agentic explicitly tests pronouns, generic descriptors, duplicate objects, omitted destinations, vague spatial relations, vague quantities, and missing criteria.

Sweep's resolvers should return exactly one of `resolved`, `ambiguous` with candidates, or `unresolvable`. The compiler must never invent a drone ID, location, heading, zone, or selection to satisfy schema. Preview should show resolved IDs and concrete spatial values rather than only echoing the original phrase.

### Indirect prompt injection

[AgentDojo](https://proceedings.neurips.cc/paper_files/paper/2024/hash/97091a5177d8dc64b1da8bf3e1f6fb54-Abstract-Datasets_and_Benchmarks_Track.html) contains 97 realistic tool-use tasks and 629 security cases where malicious instructions arrive in tool-returned data. [RIPA](https://arxiv.org/abs/2606.28649) tests injection through OCR, speech transcription, and poisoned LiDAR context in an LLM-controlled ROS 2 stack. [When Prompts Control Robots](https://arxiv.org/abs/2608.00747) tests direct and perception-path injection in multi-robot systems. [Hijacking Robots with a Piece of Paper](https://arxiv.org/abs/2608.05715) tests text placed in physical scenes across 5,670 trials and reports nonzero attack success for all three evaluated models.

These studies support PRD Section 7.2's decision to exclude detection labels, stream names, and device text from compiler instructions. That exclusion needs a field allowlist because prompt wording cannot enforce the boundary. The compiler request should contain fixed developer instructions, the deliberate operator utterance, and a typed projection of authoritative state. Raw OCR, captions, detection labels, stream names, non-operator audio transcripts, adapter errors, logs, and device metadata should never be interpolated into model messages.

If any new field later enters compiler context, record its provenance and add adversarial cases first. Quoting or delimiting untrusted text is useful for readability but does not provide authorization.

## 5. Cross-check against PRD Section 7.2

The PRD already provides the load-bearing controls: restricted model inputs, schema-constrained intents, `validate_plan`, operator preview and confirmation, deterministic planning, command-level arbitration, local video, environment-held API keys, relay authentication, and tamper-evident logs. The following items make those controls enforceable and testable.

| Gap or underspecified edge | Implementation guidance | Evidence |
|---|---|---|
| Model result has only a successful plan shape | Define typed `plan`, `clarify`, `unsupported`, and `refuse` outcomes, plus provider refusal, timeout, truncation, and malformed-output failures. Every non-plan path emits zero intents. | ASIMOV-Agentic, SafeGate, Structured Outputs |
| Schema-valid values can be wrong | Validate references, units, bounds, mode, selection, state preconditions, capability profile, and plan length after parsing. | BFCL, SayCan, Decode-Time Grammars |
| Preview can become stale | Attach state and capability versions plus a plan digest and expiry. Confirmation authorizes that exact tuple. Revalidate on confirm and before every intent. | SafeGate, industrial digital-twin system, ASIMOV runtime cases |
| A multi-step plan can become unsafe partway through | Stop remaining intents when armed state, e-stop, battery, position quality, link, geofence, selection, mode, or capability facts change. Keep the arbiter authoritative. | SafeGate task contracts, Safety Chip, ASIMOV-Agentic |
| Resolver ambiguity can be hidden by a valid schema | Return candidate options and require clarification. Show resolved IDs and concrete spatial values in preview. | DialFRED, ASIMOV-Agentic, OK-Robot |
| Perception data could drift into model context during implementation | Construct context from an explicit typed allowlist. Add a data-flow test that fails if perception or device text reaches an instruction-bearing message. | AgentDojo, RIPA, physical prompt injection studies |
| Direct injection can still request a schema-valid action | Evaluate adversarial utterances and authority spoofing. Limit speech capture to deliberate operator input and retain preview for every language plan. Physical safety remains in the arbiter. | RoboJailBench, EMBODYGUARD, AutoRT |
| Shared relay token does not by itself bind source identity | At admission, verify that the authenticated source is allowed to claim its registered source ID. Log authenticated identity separately from the payload's `source` string. | Hardening inference from the source registry and Section 7.2's authentication goal |
| Retries and replay can duplicate motion | Use per-session monotonic sequence numbers, short confirmation expiry, and idempotency keys. Reject stale or duplicate emissions before planning. | Hardening inference extending the PRD's stale-timestamp adversarial case |
| Resource exhaustion can turn failure into delay or repeated attempts | Bound utterance size, result size, plan length, resolver candidates, retries, and total confirmation lifetime. Fail closed on provider or local-model failure. | Hardening inference from the fail-closed requirement and provider failure modes |
| Audit log may omit the evidence needed to reconstruct authorization | Log provider metadata, parsed outcome, schema result, resolver result, capability verdict, plan and state digests, operator decision, each pre-dispatch revalidation, arbiter result, and adapter acknowledgement. Exclude secrets and unrestricted request headers. | SELP artifacts, industrial typed failures, PRD audit requirement |

## 6. A useful result that does not fit the categories

The recent safety benchmarks show that planning competence and safety awareness are separate properties. [DESPITE](https://arxiv.org/abs/2604.18463) evaluated 23 models and found that models with nearly perfect plan feasibility could still produce dangerous plans. This supports Sweep's decision to keep model selection, plan accuracy, capability validity, and unsafe dispatch as separate metrics.

The same distinction applies to gestures. A classifier can have high frame accuracy while emitting duplicate or accidental events during transitions. Sweep should evaluate the semantic event stream that reaches the intent boundary, then separately verify that the relay, planner, arbiter, and adapter handled those events correctly.

The practical consequence is one end-to-end eval record with several independent verdicts. A result should say whether the input was understood, whether the plan was structurally valid, whether references resolved, whether the adapter supported it, whether current state permitted it, whether the operator confirmed the exact plan, and whether execution matched the authorized result. One aggregate success number cannot locate those failures.
