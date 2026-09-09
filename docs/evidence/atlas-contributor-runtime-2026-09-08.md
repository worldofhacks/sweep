# Desktop owner and Android contributor checkpoint

## Environment

An independent desktop Chromium session and the actual `fakeDebug` APK ran against
one fresh loopback-only relay workspace. Android used the isolated AOSP API 35
ARM64 emulator with WebView 124 and its generated camera. The native bridge,
CameraX, encrypted session storage, outbox, WorkManager and HTTP path were real.
No physical phone, host webcam, microphone, aircraft or robot was used.

The workspace was `draft-audit`, with one explicitly labeled Shoal Creek / Austin
demo space: `8febd9c9-39c9-4b99-b904-65158a365a06`. The user's saved preview was
not modified. The contributor invitation was issued through the owner's existing
HTTP endpoint and entered through Android's real connection form. Credentials
were neither printed nor committed.

## Verified flow

1. The desktop owner opened the demo and selected **Coverage → Area 5:5 → Request
   a view here**. Android's independent contributor session displayed the same
   open request. Create-space was disabled; owner-only share/resolve controls were
   absent. No fleet action was attempted.
2. Android selected **Contribute this view → Use your camera**. The actual native
   camera displayed the frozen requested-view context and safety explanation.
3. The emulator's relay tunnel was removed and airplane mode enabled. One photo
   finalized privately while the relay still had zero captures. Android subsequently
   reported no active default network. The native outbox showed **Waiting to
   upload** and that the request link was awaiting confirmation.
4. Restoring the tunnel and Android connectivity automatically uploaded the photo,
   without tapping Retry. Android displayed **Saved and linked to the requested
   view**. Both clients displayed **View 1 linked capture** for area 5:5; opening it
   rendered the same generated-camera image in the requested-view capture list.
5. The owner HTTP response linked exactly capture
   `0d724225-2bde-490c-9d53-aa8c4fcedf67` to request 5:5. Its 37,987 JPEG bytes
   matched the retained native original and the server's SHA-256:
   `127cc857ba78c86ac99b99a392975c4a752a74a0f4bc12f54a1c8cbf6d710bd5`.
6. GPS remained denied: `position=null`, zero live people, zero GPS coverage.
   The request correctly remained open. A linked photo alone does not prove a
   capture location, reconstructed surface completeness or request fulfillment.
7. Replacing the disposable space's invitation through the owner endpoint caused
   the connected Android session to show **Space unavailable — This invitation
   does not grant access to the space**. The remote title, details and media were
   removed from the rendered view. The owner's access continued. All 12 private
   originals, including 11 from earlier isolated tests, remained unchanged and
   available in the device's Uploads page.

This verifies the online authorization refusal, not remote erasure of downloaded
data or metadata-cache invalidation across a subsequent offline restart. That
later lifecycle requires separate qualification. Local originals intentionally
remain owned by their contributor.

## Cached-space notice correction

The previous run showed an offline notice on Uploads after workers reconnected.
That state actually describes whether **Spaces metadata** came from cache; it is
not an OS or workspace connectivity monitor. The notice now appears only while
viewing Spaces. Uploads retains the existing per-item native states, progress,
errors and exact saved acknowledgments. No new polling, bridge operation or
network-status guess was added.

Two regressions cover queued and saved uploads: leaving cached Spaces removes its
notice, returning preserves the cached-data warning until a fresh successful
space read, and upload status is not changed by navigation. The actual emulator
also showed the cached warning on offline Spaces and no stale warning on Uploads,
both before and after automatic recovery. Existing mineral/pine colors, border
treatment, 8px spacing and layout were retained.

## Evidence and limits

Ignored local evidence lives in `output/playwright/`:

- `android-contributor-{connected,queued,linked,revoked}.png` and
  `desktop-contributor-linked.png`.
- `android-contributor-{linked,revoked}.json`, containing the exact request relation.
- `android-capture-contributor-{offline,recovered,after-revocation}.json`, containing
  local-original checksums and server capture metadata.

Both Android variants assemble, pass their 89/109 unit tests, and pass lint. The
focused web/native tests pass. Default-parallel full console runs timed out
in existing shell/flight navigation tests; the shell suite passed independently.
The complete suite then passed all 93 files / 1,260 tests with
`pnpm test --maxWorkers=2`, retaining the same five-second timeouts and assertions.
The emulator
also briefly displayed a System UI wait dialog during the concurrent workload;
the capture activity continued without restarting it. Neither event is presented
as a clean performance acceptance. Host contention was observed; unrelated
processes were not stopped. Full-suite rerun results are also in the PR handoff.

Desktop fleet/provider requests remained unavailable in this Atlas-only relay, as
expected; this run is not fleet or media-provider qualification. Physical sensors,
multiple physical handsets, HTTPS/cellular deployment, Doze/OEM restrictions,
mid-upload process loss, push delivery and geometric missing-surface inference
remain outside this check. The isolated relay, emulator and test browser were
stopped afterward; the user's port 8177 preview remained available.
