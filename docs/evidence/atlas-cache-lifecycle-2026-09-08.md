# Native metadata-cache lifecycle

## Change

Successful native HTTP detail reads now populate the bounded offline metadata
cache directly. The returned space ID must match the requested space; live people
and contributor count are removed from the cached copy, not the live response.
The redundant `cacheSpace` JavaScript operation is removed from the bundled client
and native bridge together. APK assets and native code must remain a matched build.

An observed HTTP 401 invalidates metadata for that exact immutable credential.
HTTP 403 on a space read invalidates that space; a denied directory read invalidates
the credential's directory cache. Media and upload access refusals also invalidate
the affected scope. A contributor being refused an owner-only write does not remove
otherwise authorized read metadata. Temporary server/network failures retain the
last accepted cache.

Each native detail request records the cache generation before network I/O.
Invalidation advances it, and an older response cannot publish a new cached copy
or be returned as an accepted fresh detail. A subsequent authorized read can cache
again. Invalidation deletes only derived rows in the existing SQLite cache table;
the schema stays at version 2. No dependencies, permissions, polling loops or
new persistent tables are introduced.

Captured originals, upload rows/credentials and private drafts are not removed.
This is not remote erasure: a contributor retains their own files. An entirely
offline client also cannot learn about an invitation change until it reconnects.

## Verification

Eight native regressions cover sanitized cache writes, correct space/credential
scope, authentication refusal, preservation after an operation refusal or temporary
failure, retirement of older observations, a fresh accepted read, persistence across
reopening the real SQLite database, retained originals, and media/upload scope.
The existing HTTP worker refusal test also verifies cache invalidation while
retaining its original. A web regression confirms that successful detail reads no
longer send a later cache-write message.

Both APK variants assemble and pass lint. All native tests pass: 97 fake and 117
probe. The full console suite passes 93 files / 1,261 tests with two workers;
ESLint and the production TypeScript/Vite build pass. These are software regressions,
not physical handset, storage-fault or remote-erasure acceptance.

## Normal offline runtime smoke

The updated `fakeDebug` APK ran on the isolated AOSP API 35 ARM64 emulator,
WebView 124, against a fresh loopback-only `draft-audit` relay. A valid contributor
invitation opened the Austin demo space through the actual connection form.
After removing the relay tunnel and enabling airplane mode, Android reported no
active default network and the native-cached detail remained available with the
offline notice. A force-stop and cold relaunch retained the same authorized detail.
Restoring connectivity cleared the metadata offline notice on the next live read.

This run created no media and activated no physical camera, microphone or aircraft.
All 12 private originals from earlier tests retained exactly the same checksums.
The cache-invalidation/refusal transitions above were verified by native regressions;
the emulator smoke verifies normal cache population and cold offline retention,
not an additional live revocation or storage-fault scenario.

Missing map tiles were visibly reported during the cold offline run. Their warning
was still present after metadata reconnected; this run does not qualify automatic
tile recovery or offline map imagery. Local evidence is ignored under
`output/playwright/android-native-cache-cold-start.png` and
`output/playwright/android-capture-cache-cold-start.json`. The disposable emulator
and relay were stopped afterward; the user's port 8177 preview remained available.
