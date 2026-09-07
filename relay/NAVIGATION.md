# Named destination reviews

The platform API connects the existing console's navigation pane to the approved
map store and authoritative relay state. It implements the shared #143
`navigate {zone_id}` identity and frozen review contract for aircraft, ground
robots, and mixed selections. The operator supplies a named identity. A class
planner owns coordinates, route clearance, arrival allocation and hold behavior.

The default service contains no destination fixtures, guessed coordinates,
synthetic device poses, or physical configuration defaults. It requires an
approved current world-map revision and configuration from the loaded autonomy
composition. More than one approved current map requires an explicit active-map
selection; the store refuses to silently choose one.

## HTTP contract

Every route uses the existing authenticated console bearer credential and session
binding. Responses are `no-store`. Request fields are exact, JSON is bounded and
finite, and errors return `{code, detail}` with a non-success HTTP status.

| Operation | Route suffix under `/api/sessions/{session_id}/navigation` | Request |
| --- | --- | --- |
| Current catalog | `GET /catalog` | None |
| Resolve a name or alias | `POST /resolve` | `{query, selected}` |
| Compile an explicit destination reference | `POST /compile` | `{intentId, query}` |
| Review a selected canonical destination | `POST /preview` | `NavigationPreviewRequest` |
| Revalidate the captured review | `POST /confirm` | `{previewId, intentId, previewHash}` |
| Select a map for navigation | `POST /select-map` | `{reference}` |

The shared DTOs are defined in `console/src/navigation/types.ts` and validated by
both the server and production console parser. `selected` contains device IDs,
explicit classes and connection epochs. A preview request binds the current
session, roster version, selection, canonical zone ID, accepted map reference,
catalog version and measured motion configuration.

Catalog responses contain `{status: "ready", reason: null, catalog, serverNowMs}`.
Preview responses contain `{preview, previewHash, serverNowMs}`. The hash identifies
canonical server JSON including every frozen route, arrival slot, class-specific
hold behavior and outcome. The console keeps that hash independently of its local
timestamp projection. It converts the server validity window to its own clock
conservatively, subtracting the entire request round trip. A browser timestamp
never becomes server confirmation authority.

Selecting an approved revision is a separate operator action from approving it.
Its receipt is `{reference, selectionId, selectedBy, selectedAt}`, with the actor
supplied by the authenticated host route. The exact revision selection persists
across process restarts in an immutable audit trail. Editing the selected bundle
does not automatically select its new head: the operator must explicitly select
the newly approved revision. Every selection retires prior reviews, including
A → B → A transitions. A session retains at most 4,096 selection audit records.

The name resolver uses NFKC normalization, whitespace collapse and lowercase,
matching the console. Canonical IDs and aliases participate in ambiguity checks.
Ambiguity returns candidates; unknown references and named excluded map areas
return typed refusals. The compiler accepts a destination reference, not a
multi-action transcript. It captures the authoritative current selection and
returns a review with only `navigate {zone_id}`. It never inserts selection,
takeoff, capture, survey or formation steps.

## Evidence and invalidation

The approved map reference pins the saved revision's content hash and approval
audit receipt. The static geometry and catalog pins hash the actual authoring
documents and destination entries. Their version names start with `static-` and
`catalog-`; they are not #82 generated flight-grid or class-navigation artifacts.
Static map approval does not establish route reachability, so an unqualified
destination reports `reachability: "unknown"`.

The service re-reads current state after any route provider finishes. A late result
cannot bind a changed roster, class, epoch, selection, map, configuration,
readiness, authority, emergency stop or pose evidence. Published material state
changes increment a durable generation, so A → B → A cannot revive an old review.
Map save and approval operations explicitly invalidate the session's reviews.
Process restarts also retire prior review authority. Ordinary state event IDs and
clock ticks do not create material changes on their own.

SQLite retains at most 256 unexpired reviews by default. Reviews expire after
15 seconds; this is an operator-review lifetime, not a physical motion limit.
Saved preview fields are immutable, hashes are verified on confirmation, and
every well-bound confirmation attempt is one-shot. Storage failures report a
typed unavailable result rather than a successful receipt.

## Execution boundary

C1 and C2 retain their existing executable capability sets. The registered Intent
v1 vocabulary validates the exact `zone_id` argument, nonempty selection and
explicit confirmation, then refuses navigation under those profiles. Generic
language plans also refuse navigation because they do not contain its separately
frozen map, route and configuration evidence.

Per-device review outcomes distinguish disabled relay/device capabilities,
missing readiness or control authority, grounded aircraft, excluded destinations,
and missing class-qualified routes. Grounded aircraft never acquire takeoff
permission through navigation. Ground robots retain `stop` arrival behavior;
aircraft retain `hover` behavior in qualified provider previews.

`RoutePreviewProvider` is an explicit trusted class-planner integration seam. It
may contribute bounded routes and typed outcomes with exact target and map/floor
bindings. It cannot add a dispatch flag or issue an adapter command. Production
has no such provider until its class planners and world-pose inputs are qualified.
Every current preview reports `dispatchEligible: false`; otherwise-current
confirmation returns `navigation_execution_unavailable`. This implements #143's
review contract without claiming completion of the downstream #144/#145/#249
class-planning and execution releases.
