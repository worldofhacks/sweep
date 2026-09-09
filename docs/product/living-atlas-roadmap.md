# Living atlas execution roadmap

## Goal and current checkpoint

Build an easy-to-use, consent-driven shared world and timeline that is useful for personal memories and community improvement. Preserve the existing console, speech, gestures, cameras, Android capture, and hardware-safety contracts. The product rationale and external evidence are in the [blueprint](living-atlas-blueprint.md).

Status: backend identity/membership, the browser invitation/member flow, and account-owned Space creation are implemented locally, September 9, 2026; see the [implementation checkpoint](atlas-account-checkpoint.md). LA-01 remains partial: ownership transfer, deletion/recovery lifecycle, real-provider acceptance, and native sign-in are not complete. The persistent goal is active. The first pilot is provisionally one shared Austin place, combining friends' memories with one improvement task. This assumption is reversible; it is not validated customer demand. No production social provider, paid pilot, public atlas, or new device connector is qualified by this document.

PR #338 remains held and must not be merged. This roadmap does not authorize a merge, change current issue states, or replace the hardware MVP. Existing uncommitted memory/landing changes are separate work and must be preserved.

Subsequent local progress includes the LA-02 timeline and LA-03
[account memory contributions and attributed history](atlas-collaboration-checkpoint.md).
The next gating implementation area is the consent/deletion/derived-output
lifecycle before expanding participant AI or public discovery. Real-provider,
real-device, and customer acceptance remain required rather than inferred from tests.

## Delivery rules

Each slice should be independently reviewable and small enough to diagnose or roll back. Reuse existing Atlas and memory storage, routing, styles, and job conventions. Start with the smallest contract that supports the acceptance scenario; no unused integration framework or speculative rewrite.

A slice is complete only when its outcome, failure behavior, tests, migration/rollback implications, and evidence are recorded. A stub proves interface behavior, not external-provider quality. A web build does not prove Android behavior. Local documentation of completion does not prove that a feature is on `main` or deployed.

Use a single integration owner for overlapping files. Begin read-only work and isolated tests while another slice is changing memory/UI files, but serialize shared contract and application-shell edits. Do not stage unrelated worktree changes. Safety-relevant changes require the existing independent review and real acceptance gates.

## Ordered slices

| ID | Outcome | Depends on | Current state |
| --- | --- | --- | --- |
| LA-00 | Capability audit, product hypothesis, and acceptance backlog | None | This documentation checkpoint |
| LA-01 | Verified account identity and explicit membership | LA-00 | Partial: [account-owned Spaces checkpoint](atlas-account-checkpoint.md); lifecycle/provider acceptance pending |
| LA-02 | Honest time/place model and timeline | LA-00; LA-01 for shared acceptance | Partial: [calendar timeline and date-history checkpoint](atlas-timeline-checkpoint.md); real-device/provider acceptance pending |
| LA-03 | A complete two-person contribution and revisit loop | LA-01, LA-02 | Partial: [account memory contributions and history](atlas-collaboration-checkpoint.md); real-user/device loop pending |
| LA-04 | Consent, deletion, and derived-output lifecycle | LA-01, LA-02 | Partial: [source-removal backend and web review/status](atlas-removal-checkpoint.md); native, recovery, backup and audience-policy acceptance pending |
| LA-05 | Reliable, bounded AI enrichment with evaluation | LA-02, LA-04 | Partial: [cooperative cancellation, replay-safe requests and browser reload recovery](atlas-analysis-checkpoint.md); budgets, physical-native recovery acceptance, crash recovery and qualification pending |
| LA-06 | One community task from report to reviewed resolution | LA-03, LA-04 | Planned |
| LA-07 | Pilot analytics, cost evidence, and buyer validation | LA-00; operational pilot after LA-03/04 | Planned; measurement design may start early |
| LA-08 | Curated public worlds and contributor protection | LA-04, LA-06, LA-07 | Deferred until gates pass |
| LA-09 | Qualified immersive revisits and spatial comparison | LA-03, LA-05, LA-07 | Experiments exist; product qualification pending |
| LA-10 | One justified authorized connector at a time | LA-04, LA-07 | Deferred |
| LA-11 | Healthy rewards and additional return-value experiments | LA-03, LA-07 | Local points exist; server-backed experiment deferred |

### LA-01 — Identity before social reach

Introduce a provider-neutral internal user identity mapped from a server-verified session. Integrate the existing Clerk entry point rather than adding another authentication vendor in parallel. Provider issuer/audience/expiry and account mapping belong on the server. Client-supplied contributor IDs cannot become proof of ownership. Keep social membership and fleet/operator authority separate.

Define owner, contributor, and viewer behavior, invite acceptance, expiration/revocation, account recovery, and legacy-record migration. Legacy media must not be claimed by matching a display name or guessed email. Keep current workspace access functional during a deliberate compatibility period; document which authority is accepted on each route.

Acceptance: two separate test accounts contribute to one world; neither accesses an unrelated private world. A viewer cannot edit or start a paid job. Revocation takes effect online across APIs and asset reads. Invalid/expired sessions fail closed. Existing operator credentials and invite behavior pass regressions. Production sign-in requires a real Clerk application, configured domains, and provider setup; mock tests alone cannot complete this slice. Native sign-in receives its own verified flow before being advertised.

### LA-02 — A timeline that never invents precision

Project existing authorized captures and memory context into a stable timeline. Retain capture time, import time, optional user assertion, timezone/precision, and provenance separately. Allow missing location and approximate dates through an additive contract/migration. Preserve compatibility with existing coordinate-bearing Spaces and local robot frames.

Acceptance: a late upload appears in its historical chapter; an unknown date is labeled unknown; unknown-zone EXIF does not silently become UTC; a corrected date reorders predictably without losing the original assertion. Sorting is deterministic under ties and pagination. Two users see only events they may read. Mobile list, chapter view, and map describe the same sources. No fabricated Austin marker appears for unknown GPS.

Include tests for ambiguous daylight-saving time, clock skew, approximate ranges, duplicate uploads, corrected timestamps, and stale cached data. Start with a projection, not a new event platform. The first implementation should touch neither hardware timestamps nor planner interpretation.

### LA-03 — A useful two-person world

Deliver: create a private world, add existing media, invite a friend, accept, contribute from a second account/device, and revisit the resulting chapter. Reuse existing capture persistence, original-media integrity, and resumable native upload. Add attributed edits and conflict handling, not a real-time document editor unless the pilot requires one.

Acceptance: both people can explain the audience, find the contribution, hear permitted audio on request, and return by a stable link. Interrupted upload resumes or offers an honest recoverable error without duplicate records. A non-3D chapter remains useful. Existing console routes, speech/gesture interactions, camera selection, and native camera/outbox tests pass. Real target-device contribution and playback are recorded separately from browser/JVM results.

Design: warm ivory/ocean blue, subtle depth, shared 8-pixel rhythm, one header/logo, clear primary action. Keep the hero-only landing unchanged. No added global status banner or duplicated application chrome. Advanced controls stay available without occupying the novice's entire first screen.

### LA-04 — Sharing and deletion are product features

Define the audience and retention of originals, attachments, transcripts, thumbnails, summaries, generated worlds, cached copies, exports, and backups. Track derivation dependencies. Source permissions must constrain every derived view and model input. Offer an explicit audience preview before publication and source-level controls where material is reused.

Acceptance: narrowing a source's audience removes it from future search, summaries, exports, and accessible derivatives; queued work rechecks permission; cached output cannot bypass revocation. Deleting or replacing material has visible progress and specified backup expiry. Export states what it includes and warns about sensitive location/transcript data. Explain that recipients' prior downloads cannot be remotely erased.

Public sharing stays disabled until its redaction/review behavior is tested. Use consented pilot media only. Synthetic examples are labeled and kept separate from real community reports. Do not imply complete privacy-law compliance from these engineering checks; production policy and vendor terms require a separate review.

### LA-05 — AI that earns its place

Reuse the memory context implementation. Add only enrichment needed for the first journey: metadata suggestions, selected-track transcript, optional context lookup, and source-linked chapter drafting. Show recorded, declared, estimated, and creative information distinctly. Keep missing providers and partial failures usable.

Acceptance: originals remain accessible while processing; paid work has a budget, idempotency, cancellation, and an unambiguous retry policy. Tests cover revoked jobs, unknown time/location, weather outage, incomplete speech, soundtrack exclusion, unsupported output, and private-source filtering. Logs capture usage and failure class without recording unnecessary private content.

Create a consented evaluation set before claiming quality. Record dataset/version, output provenance, unsupported claims, citation accuracy, abstention, latency, and actual cost. Real-provider acceptance is required with approved configuration; no secrets go into client bundles or repository files. Advanced sound classification, full-video analysis, and AI change detection are later sub-slices with their own evidence.

### LA-06 — One improvement, visibly completed

Use the same world and relevant captures for a bounded task. Implement submitted, acknowledged, assigned, resolution proposed, and reviewed closed states, plus reopening with a reason. Reuse existing incident/request fields where appropriate; inspect their transitions before designing a duplicate workflow. An original capture can support a task without widening the audience of its whole memory.

Acceptance: a responsible reviewer can trace a before/after outcome to originals, assign work, reject an unsupported resolution, and export an understandable record. Duplicates do not create repeated assignments. Every status has an actor and timestamp. No automatic emergency dispatch, unverified claim of official acceptance, or public accusation is introduced.

Begin with a fictional Austin garden/road-maintenance example, then a consenting partner's actual low-risk task. Existing civic tooling is the system of record where applicable; manual export and an external reference precede a custom integration. Crime and disaster submissions require additional moderation/recipient review, not merely another category toggle.

### LA-07 — Measure desire and commercial usefulness

Before onboarding, define privacy-minimizing events for saved contribution, accepted invitation, second contributor, meaningful revisit, task outcome, processing cost, and sharing correction. Do not capture raw images, transcripts, exact location, or session replay in product analytics. Use the smallest retention period that supports the pilot questions.

Run the blueprint's proposed five-group/two-team study against their existing tools. Report denominators, qualitative failures, active interaction time versus network wait, meaningful returns, task-preparation time, and a buyer's actual decision. Proposed gates are hypotheses to refine before observing results, not benchmarks to retroactively manipulate.

Acceptance: one observed end-to-end shared-memory loop, one reviewed improvement record, a repeat-use cohort report, measured low/typical/heavy workload cost, and an explicit paid-pilot decision. A negative result is valid evidence and should narrow scope. No claim of product-market fit follows from a polished demo or a small sample.

### LA-08 through LA-11 — Earned expansion

LA-08 adds an opt-in, curated public atlas only after source controls, moderation, reporting/blocking, sensitive-location treatment, and takedown paths work. Public discovery indexes approved derivatives, not private worlds. Test zoom and no-tile states and provide a non-map alternative. Start with a small Austin collection, not a promise of complete global coverage.

LA-09 qualifies reconstruction licensing, representative capture success, rendering performance, and spatial uncertainty. Keep gallery/panorama fallback and source-linked originals. Test guided revisits and user-selected before/after comparison before a generalized time-travel world. Generated geometry cannot affect robots or serve as documentary repair evidence.

LA-10 admits a connector only with a named user need, documented authorized API/import path, provider terms, bounded collection, cost measurements, and revocation/deletion tests. Phone and owner-selected media come first. Tesla live/historical media capabilities remain unqualified. Flock is not a public feed source. Never scan for exposed cameras or scrape around access controls.

LA-11 experiments with monthly chapters, safe repeat-view guidance, narrated postcards, shared milestones, and optional reminders. Server-backed points, if retained, need deduplication, caps, reversals, and no incentive for dangerous incident capture. Ship one experiment at a time and compare meaningful return value and unwanted notifications against a no-reward baseline.

## Integration with existing work

The current [MVP plan](../mvp-plan.md) and issue tracker remain the record for their respective active work. This program maps to F.3/F.6 without changing their existing hardware dependencies. These are related issue references, not a claim that they implement the new consumer work:

| Existing thread of work | Relationship |
| --- | --- |
| [#239 shared world/Ohmni epic](https://github.com/worldofhacks/sweep/issues/239), [#243 frame registration](https://github.com/worldofhacks/sweep/issues/243) | Preserve the observation/frame contracts; consumer place labels do not replace measured robot frames |
| [#93 world replay](https://github.com/worldofhacks/sweep/issues/93) | Reuse time/provenance principles; keep human memory timeline separate from safety replay semantics |
| [#24 room-world journey](https://github.com/worldofhacks/sweep/issues/24), [#25 pilot survey](https://github.com/worldofhacks/sweep/issues/25), [#59 real world-generation job](https://github.com/worldofhacks/sweep/issues/59) | Link applicable capture/generation/pilot evidence rather than duplicating it |
| [#42 audio capture](https://github.com/worldofhacks/sweep/issues/42) | Preserve command-speech behavior; memory transcription must not emit control intents |
| [#81 world bundle](https://github.com/worldofhacks/sweep/issues/81) | Preserve validated operational maps; public atlas content is not an approved bundle |

Before publishing new implementation issues, search current titles and bodies and reconcile duplicates. Do not close historical issues on the strength of this document. Re-read current PR heads and checks for any eventual integration; PR #338's hold persists.

## Initial research checkpoint record

| Evidence | At this checkpoint |
| --- | --- |
| Product/market/integration research | Written in the sourced blueprint |
| Capability audit | Documented against the inspected branch; limitations explicit |
| Prioritized implementation backlog | Defined above with dependencies and acceptance |
| New runtime implementation | None from this research checkpoint |
| Production authentication or new provider setup | Not performed |
| Real-user interviews, retention, revenue | Not measured |
| Existing application behavior requalified | Not by a documentation-only change |
| Merge/push/deployment | None from this checkpoint; #338 held |

Subsequent implementation: LA-01 has [account verification, owned Spaces, and browser membership flows](atlas-account-checkpoint.md); account deletion/recovery, transfer, and real-provider/native acceptance remain incomplete. LA-02 has a [calendar timeline, scoped date corrections, and original-date history](atlas-timeline-checkpoint.md), with desktop/phone-viewport review and a native route test. Final provider qualification requires the deployment's configured account; that external dependency must not be disguised as completed sign-in. The overall goal stays active until implementation and real acceptance evidence are delivered.
