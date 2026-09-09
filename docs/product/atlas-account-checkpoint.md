# Atlas account identity, membership, and sharing checkpoint

## Browser experience

The existing header's Clerk session now connects to a separate account client.
“My spaces” lives inside Spaces, not in another global header. Signed-in people
can create an owned Space, review an invitation, explicitly accept its permissions,
open their Spaces with the existing capture/map/world experience, and leave a
non-owned Space with a confirmation. Signing in and previewing an invitation do not accept membership.
Viewer controls do not offer capture, location publication, or capture requests;
the server independently enforces these restrictions.

Space account owners and workspace operators have explicit viewer/contributor invitation choices, 24-hour
or seven-day expiry, pending invitation revocation, and confirmed member removal.
Opening sharing settings creates no grant. Legacy contribution links remain in a
separate disclosure with explicit reveal/copy/replace actions. Permission copy
explains that membership includes current and future media and location details,
but does not grant fleet controls or paid AI execution.

Account tokens are requested from the SDK per request; they are never copied into
operator connection settings or persistent client storage. Requests are restricted
to the trusted configured Atlas API origin and Atlas account/Space routes. Account
switches invalidate in-flight results and unmount the private directory. The
one-time invitation fragment is removed from the address bar and retained only
in this tab's session storage for at most 24 hours across sign-in redirects. An
invitation cannot select a new API origin or automatically publish a capture.

The account views are lazy-loaded and follow the existing warm ivory/ocean-blue
palette, subtle shadows, and 8px spacing rhythm. Native account entry is withheld;
the existing Android contribution-link flow remains available. This is a local
implementation, not evidence of configured production sign-in or device testing.

## What works in the backend

An opt-in account layer now verifies signed browser session tokens and maps the
verified issuer/subject pair to a persistent internal account. It does not use
display names, email guesses, or browser-supplied contributor IDs as identity.
The existing operator credential remains the authority for fleet operations,
legacy workspace management, and paid processing. A new Space's account owner can
manage that Space's account membership and status without becoming a fleet
operator. No legacy data is automatically claimed by an account.

## Account-owned creation

`POST /api/atlas/account/spaces` accepts a UUID `draft_id` and the existing
`NewSpace` model as `space`. The server chooses an account-only container; the
client cannot nominate a fleet session, owner, role, public visibility, or legacy
Space to claim. Creation inserts the Space, its owner membership, and its draft
receipt in one SQLite transaction. A matching retry returns the same grant; a
changed payload with the same draft ID returns 409. Draft IDs are scoped to the
account container. A second account reusing the same UUID gets its own Space.

No anonymous contribution token is returned or invitation issued during account
creation. Each account is currently limited to 20 owned Spaces, with admission
serialized across database connections. Retries for existing drafts remain valid
at the limit. Owners can upload captures, manage viewer/contributor invitations
and members, and resolve/reopen contributions. Owner is not an invitation role.
Management writes recheck owner authority inside their commit transaction.

The creator names the Space and place, optionally adds a description, and chooses
a location via the existing map, an explicit location-permission action, or manual
coordinates. Austin is only the initial map view, never the submitted default.
No live presence is started. The form keeps a failed submission's payload and ID
unchanged so retry cannot make a duplicate after a lost response. This held draft
is in memory only: after reload, check the directory before starting another.

Owners cannot leave or be removed, including via the operator member-removal
route. This prevents an ownerless Space; it is not a completed lifecycle solution.
Ownership transfer, account/Space deletion, recovery, and retention policy remain
rollout gates. The creation screen states these limitations, tells users to keep
originals, and explains service-administrator access rather than implying
end-to-end encryption or access exclusivity against the operator.

An operator or the Space's account owner can issue a single-recipient account invitation, with a
viewer or contributor role and an expiry of 1–168 hours. The invitation is consumed
by a signed-in account. Repeating acceptance with that same account is idempotent
while the invitation remains valid; a different account cannot redeem it again.
An existing member is not silently upgraded or downgraded by another invitation.
Changing role currently requires removal and an explicit new grant.

Members can list their joined Spaces and read their captures, reconstruction
artifacts, and memory context. Contributors can upload captures, publish their
own optional location presence, and request capture views. Viewers cannot perform
these mutations. New account uploads receive a server-written `account_id`, and
their `contributor_id` is bound to it. Display names remain user-chosen labels.
Deduplicating an existing original never rewrites its authorship.

The operator or Space owner can remove a non-owner membership, and non-owner
members can leave themselves. Old
accepted invitations cannot restore removed access. Capture, presence, and request
writes recheck account role inside the database write transaction, including after
a slow upload. Original-media HTTP responses now use `Cache-Control: no-store` so
a cached response is not advertised as valid for another hour after revocation.
Already downloaded bytes cannot be recalled, including an in-progress response.

Memory editing, attachments, paid AI analysis, reconstruction scheduling, legacy
anonymous-link issuance, and all fleet operations remain operator-only. This is a
tested account-ownership foundation, not completed consumer lifecycle or
end-to-end provider sign-in.

## Two different invitation types

| Grant | Authentication | Revocation |
| --- | --- | --- |
| Existing contribution link | Possession of its opaque bearer token | Rotate the legacy Space invitation |
| New account invitation | Valid browser session plus one-time invitation acceptance | Remove membership; separately revoke unused invitations |

Removing account membership does not revoke an independently held legacy link.
Revoking an unused account invitation prevents acceptance; revoking an invitation
after acceptance does not remove membership. The sharing UI must explain these
distinctions; this checkpoint's UI includes that explanation. There is no implicit
upgrade of existing links, and no promise that revocation erases prior downloads.

## Explicit configuration

Account verification is disabled by default. Partial configuration is an error,
not an invitation to fall back to unverified identity. The relay accepts only
`Authorization: Bearer <session token>` for this account adapter; it does not read
session cookies or accept a client-selected key/JWKS URL.

| Server setting | Purpose |
| --- | --- |
| `SWEEP_ATLAS_JWT_ISSUER` | Exact HTTPS issuer origin for the configured application |
| `SWEEP_ATLAS_JWT_PUBLIC_KEY` | Actual PEM **public** verification key; RSA, at least 2048 bits |
| `SWEEP_ATLAS_AUTHORIZED_PARTIES` | Comma-separated exact browser origins; HTTPS, or explicit localhost/127.0.0.1 HTTP for development |
| `SWEEP_ATLAS_JWT_AUDIENCE` | Optional exact expected audience when the provider issues one; unexpected audiences are rejected when unset |

Use the public verification key supplied by the identity provider, not a secret
key, signing/private key, or the frontend publishable-key string. No provider
secret is needed for this networkless verification adapter. Relay CORS origins
must also match the deployment; an authorized-party setting does not reconfigure
CORS or the frontend.

The browser uses the existing `VITE_CLERK_PUBLISHABLE_KEY` and optionally
`VITE_ATLAS_API_ORIGIN` (an exact trusted HTTPS origin, or an explicit local HTTP
origin for development). With no API override it uses its own origin. Account
invitation creation is offered only when the operator's relay matches that
configured origin and the backend reports account verification enabled. A relay
URL in a pasted invitation or an operator connection is not trusted with a Clerk
token. Provider applications and redirect domains still need real configuration.

Verification permits only RS256, checks issuer, signature, expiration, not-before,
issued-at, and any configured audience, and requires bounded subject/session IDs
and an allowlisted `azp`. Pending/non-active session status is refused. Tokens must
have an issued lifetime of at most five minutes; clock tolerance is five seconds.
Missing `azp` is deliberately refused in this browser-only adapter. A native
session flow needs separate qualification, not a relaxation of the browser rule.

The key is pinned at process startup: key rotation requires updating configuration
and restarting the relay. There is no remote key discovery or provider session
introspection. Provider sign-out/revocation may therefore leave an issued token
valid until its expiration plus clock tolerance. Membership removal is checked
against local storage on each new request regardless of token validity. Production
acceptance must exercise rotation, provider sign-out, and the intended lifetime.

Capture identity is verified when a request is admitted. An admitted upload may
finish within its existing 90-second transfer deadline if that short-lived token
expires mid-transfer; its current membership is still checked in the commit
transaction. New requests with the expired token are refused. This avoids making
large uploads impossible when a provider token lasts less than the transfer.

Clerk documents public-key-based session verification and checking the token's
algorithm, expiry/not-before, and authorized party. This adapter applies stricter
browser-specific requirements and uses PyJWT for cryptographic verification, not
a custom JWT implementation. [Clerk verification documentation](https://clerk.com/docs/guides/sessions/manual-jwt-verification),
[PyJWT API reference](https://pyjwt.readthedocs.io/en/stable/api.html).

## HTTP contract

The following account routes require the signed browser token:

| Method and route | Result |
| --- | --- |
| `POST /api/atlas/account` | Enroll or return the current internal account ID and creation time |
| `GET /api/atlas/account/spaces` | Joined Spaces with their existing session IDs and roles |
| `POST /api/atlas/account/spaces` | Create or retry `{ "draft_id": "<UUID>", "space": { … } }`; returns Space ID, account container, owner role, and matching draft ID, never a bearer invitation |
| `POST /api/atlas/account/invitations/preview` | Review title, place label, role, and expiry without accepting membership or returning media/precise coordinates |
| `POST /api/atlas/account/invitations/accept` | Accept `{ "token": "…" }`, returning Space ID, session, and role |
| `DELETE /api/atlas/account/spaces/{identifier}/membership` | Leave the current account's membership only |

Space-account-owner or operator routes under
`/api/sessions/{session}/atlas/spaces/{identifier}`:

| Method and suffix | Result |
| --- | --- |
| `GET /account-invitations` | Configuration availability and pending, unexpired invitations; no invitation secrets |
| `POST /account-invitations` | Accept role and lifetime_hours; return invitation ID, one-time token, role, and expiry |
| `DELETE /account-invitations/{invitation_id}` | Prevent further redemption in this Space |
| `GET /members` | Account IDs, roles, and joined times; no provider subject or email disclosure |
| `DELETE /members/{account_id}` | Remove membership and invalidate its previously accepted invitations |

Existing detail/media/capture/presence/request routes also accept a configured
account token with the required membership; owner includes contributor capability.
The Space status route permits its account owner. Existing fleet session/list/create,
legacy invitation, paid-processing, and operator-control routes do not become account-authorized. Account routes
return 503 when identity is unconfigured; malformed or invalid tokens return 401;
missing membership/viewer writes return 403; expired/revoked invitations return
403; already-consumed-by-another-account invitations return 409. Membership and
invitation responses are not cacheable. Invitation tokens belong in an explicit
request body or carefully designed client-side invitation flow, not analytics or logs.

## Storage and compatibility

Three additive tables live in the existing Atlas SQLite database: `atlas_accounts`,
`atlas_members`, and `atlas_account_invites`. Invitation secrets are stored only as
SHA-256 digests. No social tokens or provider profile payloads are stored. Existing
capture rows are untouched; account authorship is additive on new authenticated
uploads. Existing originals keep their checksums and byte identity.

Back up the existing Atlas database and media together. An older application can
ignore these extra tables, but cannot honor the new account memberships; rolling
back disables that access path rather than translating it into a broad legacy
token. Never recreate anonymous links automatically as a rollback workaround.
When rolling back ownership support specifically, disable account verification
first: older membership-removal code does not protect an owner role it predates.

There is a cap of 50 pending, unexpired invitations per Space. This checkpoint
does not implement production account deletion, invitation-history retention,
global signup quotas, public sharing, derivative deletion, or abuse/rate-limit
policy. Those remain LA-04/production rollout gates, not implied guarantees.

## Verification and remaining work

Tests use ephemeral RSA keys and real signed tokens, HTTP routes, and SQLite
persistence. They cover invalid signatures/algorithms/claims, configuration,
membership isolation, viewer refusals, one-time acceptance, expiry/revocation,
self-leave, contributor binding, duplicate-original provenance, original bytes,
and a removed member's attempted write through a second database connection.
These are local test accounts; no real provider or user's account was contacted.

Earlier backend verification recorded on September 9, 2026:

- Full relay suite: **1,391 tests passed**. The final upload-expiry adjustment was
  made while that run was underway, so it was separately requalified below rather
  than counting the broad run as proof of that last change.
- Final focused account/Atlas/memory run: **79 tests passed**, including **35 new
  account tests** and the admitted-upload expiry case.
- Prior full console suite: **1,353 tests passed in 109 files**, before the UI slice.
- Ruff checks for changed Python modules/tests and `git diff --check` passed.
- Existing Starlette test-client deprecation and Node local-storage warnings
  remain. No production-provider, physical-device, or browser sign-in acceptance
  was performed.

Current browser-sharing slice verification:

- Full console suite: **1,370 tests passed in 112 files**.
- Focused account/Atlas/memory backend suite: **80 tests passed**, including
  **36 account tests**. Preview does not accept membership and invitation listings
  expose neither tokens nor their hashes.
- Client tests cover fresh token retrieval, account-switch cancellation, aborting
  a pending SDK request, trusted-origin restrictions, invitation expiry/storage,
  explicit preview/acceptance, viewer controls, sign-out cleanup, unavailable
  configuration, confirmed removal/leave, and the native sharing boundary.
- These tests use local mocks and ephemeral signed tokens, not real social accounts.
- Production web and Android webview builds, console ESLint, changed-module Ruff,
  and `git diff --check` pass. Existing map/world/main chunk-size warnings remain;
  no bundle-size warning was suppressed. The two sharing views are separate lazy
  chunks, about 2.4 KB and 2.3 KB gzip. No Clerk SDK chunk is emitted in the native
  build. This is not a native account sign-in implementation or a physical-device
  acceptance result.

Subsequent account-owned creation verification, September 9, 2026:

- Full console suite: **1,377 tests passed in 113 files**. A later owner-controls
  test was added and checked separately in the Spaces module suite (**9 passed**).
- Full relay regression suite: **1,395 tests passed** after the ownership changes.
- The account/legacy-Atlas/draft subset passed **53 tests**, including 38 account
  cases. Coverage includes isolated owner creation, account-scoped idempotency,
  no automatic invitation, wrong-Space/role refusals, non-owner management refusal,
  owner capture/status/membership management, sole-owner protection, atomic
  rollback of a failed membership insert, the 20-Space limit, persistence, and
  unchanged legacy authority.
- New browser cases cover explicit location selection, no implicit publication,
  frozen retry payloads, coordinate fallback, owner directory/control visibility,
  no owner-removal or anonymous-invitation UI, and map buttons that cannot submit
  the creation form. Web and Android webview builds, ESLint, Ruff, and diff checks
  pass. The creation view is lazy-loaded (about 2.2 KB gzip) and reuses SpaceMap.

No production Clerk application/domain/provider configuration was verified here.
Participant memory editing, friendly member profile names, ownership transfer,
account/Space deletion and recovery, and native sign-in are still pending. The Mac
was locked during attempted browser inspection, so visual/phone-layout acceptance
and real provider sign-in remain unverified. Browser bundling is not device proof.
No real-person invitation, external access grant, paid request, push, merge, or
deployment was performed. PR #338 remains held.

LA-01 is **partial**, not complete. The client session adapter, explicit
invitation/member UX, and account-owned Space creation are implemented; the next
slices include the recovery/deletion lifecycle. The existing console and
hero-only landing are preserved. No additional provider or integration framework
was introduced.

Subsequent timeline work is recorded in the
[timeline checkpoint](atlas-timeline-checkpoint.md), including date corrections,
updated regression results, and desktop/phone-viewport inspection after the Mac
became available. That visual inspection does not establish real-provider account
sign-in or physical Android acceptance; those gaps above remain open.

Subsequent [shared-memory contribution work](atlas-collaboration-checkpoint.md)
enables signed contributors to edit their own capture context and attach recordings,
with owner assistance and attributed history. Participant AI/weather execution,
deletion/recovery, and real-provider/device qualification remain unfinished.
