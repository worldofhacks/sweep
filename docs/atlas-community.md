# Community experience checkpoint — September 9, 2026

This focused change is on `codex/atlas-community`, based on hardware checkpoint
`32feab22`. It does not merge PR #338, publish a new PR, or change the original
integration worktree. The owner's hold on merging #338 remains in place.

## Ready for review

- Warm ivory and ocean-blue shared tokens, one logo in the persistent header,
  friendlier sidebar, and the same shell around all ten console modules.
- Spaces is the initial console page. Speech, gesture, search, multi-view capture,
  live detection, fleet services, safety controls, and navigation remain wired in.
- A short participation guide and three clearly labeled Austin starter stories:
  Shoal Creek, East César Chávez, and Mueller Lake Park. Each includes a purpose,
  invitation, and three concrete capture ideas. Illustrations are code-native SVG,
  not photographs, reconstructions, or claims of live community activity.
- Starter stories open private drafts using the existing persistence/publication
  flow. They never publish automatically or overwrite an existing draft.
- Searchable examples, local bookmarks and a Saved filter, and a contributor strip
  derived from actual workspace captures. Display names are self-supplied, not
  verified identities or live location claims.
- Private perspective points: 10 per confirmed original and 5 for answering a
  capture request. Original checksums deduplicate repeat uploads in a workspace;
  badges recognize 1, 3, and 10 originals. Reporting incidents, sharing GPS, and
  bookmarking earn no points.
- City-scale example discovery with natural street-map colors. Detail/survey zoom,
  real-tile overzoom through zoom 22, attribution, and map error/retry behavior remain.

## Clerk: wired for web, not activated

`AccountButton` loads `@clerk/react` only when
`VITE_CLERK_PUBLISHABLE_KEY` is configured. Without it, Join in explains that sign-in
is not configured; the provider names are not fake working buttons. The native
Android bundle does not import the web Clerk SDK.

Owner setup:

1. Create a Clerk application and configure its allowed application domains and
   social connections for Apple, Google, and X/Twitter.
2. Set the **publishable** key in `console/.env.local`, using `console/.env.example`
   as the template, then rebuild/restart the web console. Never put a Clerk secret
   key or provider client secret into a `VITE_` variable or source control.
3. Verify each enabled provider, cancellation, account linking, session restoration,
   and sign-out against the actual deployment. Production providers may require
   their own developer applications/credentials; follow the provider-specific
   instructions shown by Clerk. Development setup is not production readiness.

References: [Clerk React quickstart](https://clerk.com/docs/react/getting-started/quickstart),
[social connections](https://clerk.com/docs/guides/configure/auth-strategies/social-connections/overview),
[Android social connections](https://clerk.com/docs/android/guides/configure/auth-strategies/social-connections/overview).

Important boundaries:

- A Clerk session currently connects the header account UI only. It does **not**
  authenticate a relay request, replace contributor IDs, or grant private-space or
  fleet access. Existing workspace invitations/authorization are unchanged.
- Native Android OAuth still needs its native browser/SDK callback flow and
  provider configuration. The embedded WebView deliberately does not pretend that
  the web provider flow works there.
- Bookmarks/points are device-local, scoped by workspace and contributor ID. They
  do not sync to Clerk, have no monetary value, and are not an abuse-resistant
  reputation ledger. Progress is recorded when a space with one's confirmed
  captures is opened. Storage clearing can remove it; uploaded originals remain.
- Account-bound server identity, opt-in migration/linking of existing contributions,
  durable reward accounting, and cross-device synchronization remain separate work.
  Do not use local points or a client-supplied contributor ID for authorization.

## Verification

- `pnpm lint` passes.
- `pnpm test`: **106 files, 1,329 tests pass**, including draft preservation,
  storage-failure feedback, contribution deduplication, workspace isolation,
  unconfigured/native sign-in states, Clerk UI adapter tests, and shared-shell
  continuity across all ten modules. Clerk adapter tests mock the SDK; they are
  not proof of a real provider login.
- `pnpm build` and `pnpm build:android` pass. Existing large main/map/3D chunk
  warnings remain. The optional web Clerk chunk is approximately 31 KB gzip;
  it is absent from the Android output. No new UI framework or raster asset bundle.
- Browser check against real local Atlas routes: example → private draft → publish
  `Preview · Shoal Creek neighbors`; bookmark survives reload; importing a local UI
  screenshot produces one saved capture and 10 points; the contributor appears.
  This screenshot is test data, not a Shoal Creek scene capture, and has no invented
  GPS or capture time. Points also survive reload.
- Browser navigation checked Control, Live, Gesture, Speech, Search, Captures,
  Worlds, Devices, Map, and Spaces; all retain the single shared header.
- Desktop and responsive phone layouts inspected. No APK was installed, no physical
  device commands were sent, and native OAuth/hardware capture were not retested.

## Local manual review

The loopback preview at `http://127.0.0.1:8177/` serves this worktree's built web
console using isolated `.sweep/community-preview/atlas` data. It has real Atlas
routes but **no fleet relay, media runtime, or reconstruction worker**. Therefore
unavailable platform/media/WebSocket diagnostics are expected; this is not a live
hardware readiness demonstration. The ephemeral bootstrap credential is local and
not checked in. Other worktrees and their preview data are unchanged.

Review screenshots are local artifacts under `output/playwright/`, not committed
application assets. Start with Examples, open a story, read How it works, and review
Your journey. Join in explains the pending account configuration.
