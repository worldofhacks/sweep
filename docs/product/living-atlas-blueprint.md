# Sweep: a living atlas, built together

## Product thesis

Sweep should help people **remember a place together, understand how it changes, and take care of it**. The product is a shared, time-aware home for real experiences—not a requirement to continuously record life, and not a collection of unrelated AI tools.

The everyday promise is simple: bring a few photos, a clip, or a voice note; invite someone who was there; come back to something more meaningful than a camera roll. The commercial extension is equally concrete: collect a place's history, compare conditions, and document work through to a reviewed outcome. Both use the same captures and timeline, but never assume the same audience or permissions.

The recommended first pilot is one shared Austin place with a small group, a recurring gathering, and one improvement task. A community garden, for example, can hold a picnic memory, a contributor's recorded sounds, a season of growth, and a repaired entrance. This is a testable starting hypothesis, not demonstrated demand or a commitment to launch several markets at once.

Three conclusions govern the program. First, useful shared history must work before immersive reconstruction is ready. Second, reliable identity and permissions must precede broad social distribution. Third, long-term value must be measured in meaningful revisits and completed work, not raw uploads, notifications, or time spent scrolling.

## Evidence and the opportunity

Collaboration, place-based memories, generated worlds, and civic workflows are not empty markets. Apple's Shared Photo Library supports a person and up to five others contributing and editing shared photos. Polarsteps Travel Together lets trip participants contribute photos, videos, and writing while one person tracks the route. These establish a substantial baseline for collaborative memories; they do not establish demand for Sweep.[^1][^2]

World Labs' Marble accepts several input types for world creation and provides exploration, composition, and export tools. Matterport describes spatially contextual facility documentation and maintenance records. SeeClickFix already offers assigned requests, status updates, photographs, map locations, duplicate handling, and exports. “Photos plus a map,” “AI creates a world,” and “report a pothole” are therefore insufficient differentiators.[^3][^4][^5]

| Existing alternative | What Sweep must earn beyond it |
| --- | --- |
| Shared photo library and group chat | A shared story people can find and revisit with less organizing effort |
| Collaborative travel journal | A useful history of the same place across many occasions, not only one trip |
| World-generation tool | Grounded source context, ongoing contributions, and useful results without generation |
| Facility digital twin | Lightweight contributions from ordinary participants and a demonstrably better repeat workflow |
| Civic request system | Rich before/after context that helps an existing responsible team act, without duplicating its entire system |

The proposed differentiation is the connection between **shared evidence, time, and participation**. A memory becomes more complete when a friend adds another viewpoint. A later visit reveals what changed. A community task uses the relevant before/after captures, not an unrelated ticket attachment pile. These are product hypotheses to test against the alternatives above, not a proven moat.

The strongest defensibility would come from a trusted contribution workflow, permission-aware longitudinal data, quality evaluation, and adoption in recurring real tasks. A model API wrapper, a large feature menu, or unrestricted accumulation of other people's footage is not a defensible product strategy. Private user material should not become training data by default.

## What is already present—and what is not

The inspected checkpoint is branch `codex/atlas-community`, based on commit `9fdd07f6`, with ongoing uncommitted memory and landing work. This is not a statement that these capabilities are deployed on `main`. Existing implementation reports provide useful evidence but their historical test counts are not a fresh qualification run.

| Area | Evidence at the checkpoint | Missing product proof |
| --- | --- | --- |
| Spaces and original captures | Persistent originals, checksums, private drafts, scoped invitations and revocation | Account-bound ownership, social membership, and repeat multi-person acceptance |
| Friendly shared shell | Warm ivory/ocean-blue direction, starter stories, shared navigation | Novice usability study and measured mobile performance across complete journeys |
| Sign-in | Optional Clerk header integration | Server-verified identity, authorization integration, production provider setup, native sign-in |
| Memory context | Notes, declared feelings, audio/video attachments, metadata review, optional weather and AI suggestions | Full video/audio understanding, real-provider quality evaluation, shared participant editing |
| Android | Original capture/import and persistent upload recovery foundations | Target-device acceptance for the complete new collaborative journey |
| Spatial output | Documented sparse reconstruction and experimental textured surfaces | Production engine qualification, general capture success, and commercial license clearance |
| World replay | Time-aware robot/world observation replay foundations | A user-facing memory timeline and collaborative history model |
| Rewards | Local contribution points and bookmarks | Account-backed persistence, abuse controls, or evidence that points help retention |

These distinctions are documented in the Atlas, community, memory, and replay records.[^6][^7][^8][^9] In particular, the Clerk UI does not authenticate relay calls; existing contributor strings are not verified people. Existing space invitations are useful capability links, not a complete social graph. Local bookmarks and points should not be presented as synchronized accounts.

The current `NewSpace` contract requires coordinates and uses incident/hazard/community/survey categories. That is a mismatch for an old family memory with no known location. The next model must support an unknown or approximate place without invented GPS. Existing capture and memory contracts should be extended through migrations and compatibility tests, not replaced wholesale.[^10]

The existing MVP remains a technical and hardware demonstration plan. Its F.3 work already anticipates time-indexed rescans and Atlas integration; F.6 covers production hardening. This program adds a consumer/community product track rather than silently deleting hardware acceptance gates or making social features prerequisites for flight. Current issue links belong in the execution roadmap; older inventory counts are historical snapshots.[^11]

## A coherent everyday experience

The entry point should ask one question: **“What would you like to remember?”** Uploading from the device library or taking a photo should work immediately. A person can add a place or approximate date later. Account creation should become necessary at the point of durable sharing and joining, not before someone understands the product through a safe example.

After upload, show the actual saved material first. Offer a short review sheet: suggested title, possible time/place, optional sound or narration, and the audience. Explain uncertainty inline: “Date unknown,” “Location you selected,” or “Weather estimate for this hour.” Never hold the original hostage to an enrichment queue. An optional AI action must not be required to finish a contribution.

The world itself should have a small set of task-oriented views: **Remember**, **Compare**, and **Help**. These are proposed labels to usability-test within Spaces, not a mandate to rename every existing console module. Remember holds chapters and media; Compare shows selected moments side by side; Help shows a bounded task and its progress. People can move between them without re-uploading the same source.

Keep the existing console available for operators, including Control, Live, Gestures, Speech, Captures, Worlds, Devices, and Map. Progressive disclosure can make advanced tools less prominent for newcomers without removing functionality or changing existing deep links. Community identity must never automatically grant fleet permissions.

The design direction remains warm ivory and ocean blue with restrained coral or sun-yellow accents for invitations and moments of delight. Use one logo, one persistent header, shared spacing, and consistent controls. Subtle depth and an 8-pixel spacing rhythm should organize the app; status meaning must not depend on color. Preserve the approved hero-only landing page rather than restoring a long marketing page.

Target comfortable 44-pixel touch controls, visible keyboard focus, captioned video, transcripts, user-initiated sound, reduced motion, and a list alternative to maps or 3D. WCAG's target-size criterion specifies a smaller minimum with exceptions; the larger product target does not by itself constitute accessibility compliance.[^12] Use the same readable content and actions when WebGL fails or a user cannot navigate spatially.

For maps, keep a basemap at supported zooms when its provider is available; test layer visibility, resize, projection, and zoom limits. A tile failure must produce a clear retry/offline state and a usable list, not blank unexplained space. Do not promise live map tiles without connectivity. Unknown location is a legitimate state, not a marker at Austin's center. All illustrative scenarios should be labeled examples, with Austin locations only where relevant.

## The first complete story

Consider a fictional “Shoal Creek garden, together” example in Austin. Maya creates a private world from three pictures and a short video. Sweep saves them and proposes a chapter; she confirms the date and adds, “It felt like the first cool evening after summer.” That feeling is her statement, not a model's conclusion about her expression.

Maya sends a scoped invitation through her preferred messaging app. Alex joins and adds a voice note about the gathering. Each contribution retains its author and source. They choose a photo sequence with recorded ambience; if a reconstruction is available, it becomes an additional view. Their first useful result is not dependent on acquiring a robot, granting background location, or generating a perfect 3D scene.

A month later, someone photographs a damaged entrance from a safe location. It becomes a private task for the garden's responsible group. A volunteer accepts the work, adds an after photo, and a designated reviewer marks the repair complete. The memory remains private to its original audience; the repair's public summary contains only separately approved material.

The group can revisit the garden in spring, compare seasons, and export its history. A public atlas may later include the approved garden story, but never the entire private world by inheritance. This single example demonstrates personal meaning, collaboration, change over time, and an operational outcome without conflating their permissions.

## AI that reduces work without inventing history

The useful AI sequence is incremental: inspect metadata, suggest grouping, transcribe selected audio, retrieve permitted contextual data, and draft a short source-linked chapter. A suggestion needs its source references, relevant time range, processing version, and review state. Keep deterministic extraction separate from model interpretation.

Three presentation categories should be visible wherever they matter:

| Category | Example | Required treatment |
| --- | --- | --- |
| Recorded or contributor-declared | Original clip; “I remember feeling hopeful” | Preserve source and attribution; a declaration is not independent verification |
| Estimated or inferred | Weather model value; possible sound event; suggested scene description | Name the basis, uncertainty, and available source; allow correction or rejection |
| Creative reconstruction | Filled scenery, synthesized ambience, imagined transition | Explicit creative label; never counted as evidence of what happened |

Weather is context, not a recovered sensation. Open-Meteo historical data uses reanalysis, combining observations and models; resolution and time coverage vary by dataset.[^13] Store the requested location/time, dataset, units, returned grid context, and retrieval time. Do not call an hourly grid estimate the exact wind at a microphone, and do not derive capture-time weather from the uploader's current position.

Audio should preserve the distinction between original ambience, narration, and a soundtrack. Transcripts may miss speech, names, overlapping speakers, and background context. Proposed sound-event recognition should abstain when uncertain and must not convert a bang into a claim about a crime. Commercial music should start as a user-supplied reference link or a rights-confirmed recording, not an assumed entitlement to download or redistribute a song.

Time needs explicit semantics: capture time, import time, contributor-asserted event time, and processing time are different. An old imported photo must not become an event from today. Unknown timezone, approximate month, conflicting clocks, and corrections must remain representable. Show a date range or “date unknown” instead of manufacturing precision.

Spatial understanding is a separate qualification step. COLMAP describes reconstruction from overlapping viewpoints and recommends textured scenes, suitable illumination, and visual overlap.[^14] Sparse casual photos may be enough for a useful chapter but not a trustworthy reconstruction. Gate 3D on input suitability and measured results; fall back to a panorama, gallery, or annotated map when necessary.

For change comparison, begin with user-selected before/after captures of the same place. Show both originals and explain camera-angle or lighting differences. Add AI candidate changes only after a labeled evaluation demonstrates acceptable false positives and abstention. A generated before/after rendering must never be submitted as documentary repair evidence.

“Ask this world” can later answer questions such as “What did we change at the entrance?” using only sources the requester may access. It should cite the corresponding captures or task updates and say when it cannot answer. Permission checks must occur before retrieval and model submission, not only when rendering citations. No inference of hidden relationships, identities, or people's movements is needed to deliver this value.

Content credentials can supplement source integrity where available. C2PA explicitly distinguishes provenance from proof that content is true.[^15] A checksum means particular bytes have not changed; neither it nor a signed manifest establishes the truth of an incident or the identity of a person depicted.

## Collaboration, connection, and healthy return visits

Start with invitations and small circles, not a universal imported friend graph. Roles should be easy to explain: owner, contributor, viewer, and task reviewer where needed. A contributor can correct their own material; editing another person's account of an event should create a proposed change or attributed revision. Basic revision checks are sufficient initially; real-time collaborative document machinery can wait for observed need.

Clerk remains a reasonable integration candidate because its web entry point already exists. Complete backend identity verification and account mapping before presenting sign-in as shared ownership. Apple, Google, and X are desired sign-in choices, subject to each provider's production setup. Clerk documents shared development credentials for X and custom production configuration; authentication and additional social-data scopes are separate.[^16]

Do not promise ordinary Instagram account authentication or automatic friend import. Meta's published collection describes professional accounts for Instagram Login, and explicitly excludes consumer accounts from its Facebook Login API path.[^17] For the first release, invitation links shared by the user are the dependable cross-network mechanism. Future account linking must have a concrete supported API use case and minimal permissions.

Return visits should carry new value: a friend's contribution, an optional anniversary reminder, a monthly chapter, or evidence that a task was resolved. Give people a clear end to a visit and configurable digests. Do not penalize missed days, create anxiety about relationship scores, or reward capturing risky incidents.

Mild gamification can acknowledge a group's completed story or helpful task review. Prefer shared milestones and a thank-you over competitive contribution volume. If points become account-backed, award them on the server using validated actions and idempotency, with caps and reversals. Measure whether they improve useful collaboration; remove them if they encourage spam. No monetary value or transferable reward economy is required.

Additional attractive features should deepen this same loop. A “same spot again” guide can help repeat a safe photo angle. A voice-led memory postcard can let someone who cannot attend experience a gathering. A jointly edited monthly chapter can reduce organizing work. A family history export can preserve material outside Sweep. A public, reviewed place guide can explain seasonal changes. None requires continuous location tracking or face recognition.

## Community good without a surveillance product

Community work should have an explicit purpose and a responsible recipient. A report moves from submitted to acknowledged, assigned, resolution proposed, and reviewed closed; it can be reopened with a reason. Keep the reporter's assertion distinct from a verified outcome. Existing civic systems already manage many of these steps, so begin with export and a recorded external reference rather than implying official dispatch.[^5]

For road damage or disaster documentation, preserve original files, known time/place, contributor notes, and corrections. Record missing information honestly. Label reports as community submissions and avoid any promise of emergency monitoring, official acceptance, or insurance eligibility. Emergency situations need a clear direction to appropriate emergency services, not a gamified task flow.

Crime-related material needs more restrictive treatment than a park improvement: private reporting, moderation, sensitive-location protection, and a defined recipient. Do not build public accusation boards, identify suspects with AI, rank dangerous people, or assemble face/plate movement histories. These features are unnecessary to help a contributor preserve and voluntarily share a relevant original.

Effortless capture should mean a private inbox, resumable imports, and reusable consent settings for a narrowly chosen source. It must not mean silent publication, default background recording, or indefinite collection. A user should be able to stop a connector, inspect its recent imports, change retention, and remove material and its derivatives.

## Integration feasibility and sequencing

| Integration | Useful first path | Qualification before expansion |
| --- | --- | --- |
| Phone photos, videos, audio | Explicit upload/capture and private review | Real-device permissions, interrupted transfer, deletion, and accessibility |
| Social accounts | Supported sign-in plus user-shared invitations | Production provider setup; separate scopes and consent for additional data |
| Weather | Existing optional context lookup after confirmed time/place | Deployment usage terms, units, provenance, unknown-time and outage handling |
| Tesla | Evaluate owner-supplied recordings and a narrowly scoped authorized connector | Actual endpoint/media support, vehicle eligibility, costs, retention, and revocation |
| Robots and private cameras | Existing authorized capture adapters or owner-selected imports | Device-specific access, capture scope, operator acceptance, and no implicit command authority |
| Public media | Explicitly licensed imports or permitted embeds | Source permission, attribution, deletion process, and terms for the intended use |
| Flock | No general public ingestion feature | Specific approved partnership and purpose, if one is ever justified |

Tesla's Fleet API distinguishes user/partner access and scopes; its current scope table mentions Live Camera under vehicle commands. This does **not** establish that Sweep has a general historical or live video ingestion endpoint. That capability remains unqualified, and a broad command scope is not justified merely to enrich a memory.[^18]

Flock describes controlled, account-bound access and says the public cannot search its law-enforcement system.[^19] A camera being reachable online does not make it an authorized public source. No discovery of exposed cameras or collection of misconfigured feeds belongs in this program.

Do not add ten connectors before validating one recurring need. Each connector should terminate in the same bounded capture contract, with source, time uncertainty, rights, and deletion behavior. Operational telemetry and consumer memories remain different data products even when they share an ingestion pattern.

## Engineering approach and debt controls

Extend the existing application. Keep originals, captures, spaces, and memory context as the backbone. Add explicit membership, revisions, source relationships, and timeline projections only where a tested journey needs them. Avoid a second competing world database, a parallel auth model, or a large plugin framework for hypothetical integrations.

The first timeline can be a deterministic projection of authorized captures and context, not an event-sourced replacement for all existing storage. An event needs a stable ID, source references, type, time semantics, authorship, audience, and revision. A place may be absent, approximate, geographic, or a separately identified local frame; do not silently convert between local robot coordinates and geographic coordinates.

Use optimistic concurrency for notes and task transitions, idempotency for imports and paid jobs, and bounded retry behavior. Keep SQLite for the bounded local deployment while testing backup/restore and contention. Choose a production database, durable queue, and object store when deployment requirements justify them; do not introduce microservices, Kafka, a vector database, or CRDTs merely to appear sophisticated.

Sharing is a policy evaluated at read, export, search, and processing time. Derivatives need references to every contributing source and its applicable audience. Narrowing source access must invalidate dependent outputs; a generated summary must not leak a revoked contribution. Already downloaded files cannot be remotely erased, so invitation and export copy must explain that limit. Define retention for originals, thumbnails, transcripts, generated worlds, caches, logs, backups, and provider submissions.

Production processing needs per-world and per-account budgets, cancellation, job leases or an equivalent durable execution contract, safe retries, and instrumentation of actual provider usage. If a paid operation's outcome is ambiguous, reconcile it rather than issuing a blind second charge. Permission must be rechecked before queued work begins and before its result becomes readable.

The experimental reconstruction record identifies a commercial license-review gate in its engine/build dependencies.[^6] Do not make that engine a default production dependency before it is cleared or replaced. Generated assets remain presentation only: they never supply occupancy, geofence, clearance, localization, or authorization for a robot.[^11]

Every addition should have one explicit user job, an existing-module reuse decision, a failure state, a measurable exit, and an owner of removal or migration. If a feature only adds another card to the dashboard and cannot demonstrate reduced effort or increased usefulness, it should not be built yet.

## Commercial model and evidence required

The initial buyer hypothesis is a small community or property team that repeatedly documents shared spaces and coordinates improvements. It has a recurring operational task, while residents or volunteers contribute through the simpler social experience. Friends and families remain the usability and emotional-value test, not an assumed source of immediate subscription revenue.

Start with a bounded personal offering and a paid team pilot that includes administration, history, exports, and a processing allowance. Charge for durable useful workflows and storage/processing consumption, not for unrestricted access to people's lives. Consumer subscriptions, creator experiences, enterprise connectors, and an API are later experiments—not four simultaneous business models.

The unit-cost model should include retained originals and derivatives, request operations, media decoding, transcription/vision usage, reconstruction compute, delivery, authentication, moderation, and support. Instrument these per successful world and per active team. Cloudflare R2 currently lists standard storage at $0.015 per GB-month, with separate request charges; that illustrates why object storage alone is not a complete cost estimate.[^20] No provider selection or commercial price is committed here.

For example, a team retaining 100 worlds at an assumed average of 0.5 GB each retains 50 GB before backups and additional derivatives. That is an illustrative workload, not measured customer behavior. Build low/typical/heavy scenarios from pilot observations, including repeated generation and large videos. A generous “unlimited AI” tier is premature until costs and abuse are measured.

Investors should see a narrow entry market, a real repeat-use cohort, a buyer with a budget, and measured economics. Product engineers should see a complete journey, intelligible defaults, and compatibility evidence. AI engineers should see source-grounded outputs, evaluation sets, abstention, and bounded failure handling. Ordinary people should see their own memories and their friends—not a pitch about world-model infrastructure.

The pitch can be: **“Sweep turns the photos and stories people already have into shared places they can revisit, understand, and improve over time.”** The technical depth supports this sentence; it does not need to appear in the first screen.

## Validation and release gates

Use a small, consented pilot before scaling. The proposed research sample is five small groups and two community/property teams. It is a discovery sample, not statistical proof. Observe them using their current shared album/chat/spreadsheet workflow first, then compare the same tasks in Sweep. Ask for a real budget conversation or paid pilot decision rather than hypothetical enthusiasm.

Proposed gates are deliberately falsifiable: at least four of five novice participants complete a first contribution and invitation without coaching; median active interaction to that result is at most three minutes excluding transfer/processing wait; no participant leaves with a mistaken understanding of the audience. Treat even one serious sharing misunderstanding as a design defect to resolve before expansion.

For repeat value, seek at least three of five groups making an unprompted meaningful return in the fourth week, such as adding a new chapter or revisiting one with another member. Report the small denominator and reasons for attrition. For team utility, measure time to assemble a before/after record against the existing workflow and seek a concrete paid pilot commitment. These are proposed decision thresholds, not forecasts or current results.

For AI, construct a consented evaluation set covering incomplete metadata, timezone conflicts, noisy speech, sparse views, lighting changes, mistaken grouping, and denied sources. Measure unsupported factual claims, citation correctness, change false positives, abstention, latency, and cost separately. Critical acceptance cases require no unauthorized source exposure and no creative material presented as recorded evidence. Report the sample size and failures; passing a finite set is not a claim of universal accuracy.

For engineering, test account and invitation boundaries, two-device contributions, offline recovery, revoked queued work, migration/rollback, backup/restore, and original-byte integrity. Run the existing console and Android regressions appropriate to each change. A browser build is not physical-device acceptance, and a simulation is not hardware proof.

The rollout order is foundations, a complete shared-memory loop, controlled AI enrichment, one community workflow, then curated public discovery. Device connectors and advanced immersion enter only when a pilot exposes a concrete need. The accompanying roadmap defines executable slices and their evidence. If retention or buyer value is weak, narrow the use case before expanding the surface area.

## Sources

External documentation was checked on September 9, 2026. Vendor pages establish advertised or documented capabilities, not independent evidence of customer demand or comparative performance. Repository records describe the inspected checkpoint and must be revalidated as implementation advances.

[^1]: Apple. [What is iCloud Shared Photo Library in Photos on Mac?](https://support.apple.com/en-gb/guide/photos/pht153ab3a01/mac). Shared-library membership and contribution capabilities.
[^2]: Polarsteps. [I'm invited to a Travel Together trip. How is this different from tracking my own trip?](https://support.polarsteps.com/hc/en-us/articles/24266960463890-I-m-invited-to-a-Travel-Together-trip-How-is-this-different-from-tracking-my-own-trip). Group contribution and route ownership.
[^3]: World Labs. [Welcome to Marble](https://docs.worldlabs.ai/). Supported creation inputs and product workflows.
[^4]: Matterport. [Facility Document Management: Unlocking Accessible Facility Oversight Using Digital Twins](https://matterport.com/learn/facilities-management/document-management). Vendor-described spatial documentation and maintenance use cases.
[^5]: CivicPlus. [Requests List Overview](https://www.civicplus.help/seeclickfix/docs/requests-list-overview), updated April 11, 2026. Roles, assignments, statuses, media, and exports.
[^6]: Sweep. [Atlas product and implementation ledger](../atlas-product.md). Persistence, original media, native workflow, reconstruction evidence, and remaining license/acceptance gates.
[^7]: Sweep. [Atlas community checkpoint](../atlas-community.md). Shared visual direction, starter examples, optional Clerk entry point, and explicit identity/points limitations.
[^8]: Sweep. [Capture memory context](../memory-context.md); implementation in [memory routes](../../relay/memory_routes.py) and [memory store](../../relay/memory_store.py). Source context, provider opt-in, current limits, and incomplete capabilities.
[^9]: Sweep. [World replay](../world-replay.md). Observation/replay foundations and spatial meaning.
[^10]: Sweep. [Atlas contracts and store](../../relay/atlas.py), [Atlas routes](../../relay/atlas_routes.py), and [Clerk account UI](../../console/src/community/ClerkAccount.tsx). Required space coordinates, capture metadata, current authorization boundary, and UI-only sign-in integration.
[^11]: Sweep. [MVP plan](../mvp-plan.md), especially F.3 and F.6. Future spatial capture, production concerns, and retained hardware-safety boundaries.
[^12]: W3C. [Understanding SC 2.5.8: Target Size (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html). Accessibility criterion, not a complete conformance assessment.
[^13]: Open-Meteo. [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api). Reanalysis sources, dataset resolutions, temporal coverage, and usage modes.
[^14]: COLMAP. [Tutorial](https://colmap.github.io/tutorial). Image-based reconstruction pipeline and capture suitability.
[^15]: C2PA. [C2PA and Content Credentials Explainer, version 2.2](https://spec.c2pa.org/specifications/specifications/2.2/explainer/Explainer.html), section 7.2.2. Limits of provenance as evidence of factual truth.
[^16]: Clerk. [Add X/Twitter v2 as a social connection](https://clerk.com/docs/guides/configure/auth-strategies/social-connections/x-twitter), updated September 8, 2026. Development/production configuration and additional OAuth scopes.
[^17]: Meta. [Instagram API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-66f145c2-29b1-4d97-afbd-5710369027c0). Account eligibility and API limitations for both login paths.
[^18]: Tesla. [Fleet API authentication overview](https://developer.tesla.com/docs/fleet-api/authentication/overview). Token types and scopes; not proof of a qualified Sweep camera connector.
[^19]: Flock Safety. [Law Enforcement Data Access](https://www.flocksafety.com/trust/law-enforcement-access). Approved access, purpose restrictions, logging, and lack of general public search.
[^20]: Cloudflare. [R2 pricing](https://developers.cloudflare.com/r2/pricing/). Standard storage and operation pricing; rates are subject to change.
