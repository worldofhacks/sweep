# Native capture and offline recovery checkpoint

## Environment and scope

Tested the actual `fakeDebug` APK on the isolated `Sweep_Atlas_Audit_API35`
AOSP API 35 ARM64 emulator, Android WebView 124. CameraX used the emulator's
generated camera, not a host webcam. Audio was disabled. No physical phone,
microphone, aircraft or robot was used. The disposable `draft-audit` relay had
one explicitly labeled Shoal Creek / Austin demo space; the user's saved preview
and incident data were not used or changed.

This exercised the bundled WebView, native camera, private originals, SQLite
outbox, WorkManager and real relay HTTP. No simulated native bridge or injected
capture records were used. Location remained denied: every new capture had
`position=null`, and reported GPS coverage remained zero.

## Reproduced fix

After saving a video, switching to 360 scan incorrectly offered **View 2 of 8**
before any scan photo existed. One shared media counter advanced scan guidance.
`AtlasCaptureProgress` now tracks total captures separately from finalized scan
views. The saved item's kind determines which counter advances; switching modes
does not skip a view. Existing mineral/pine styling, controls and spacing remain
unchanged. Three regression tests cover mixed media and two eight-view rounds.

On the fixed APK, a video followed by scan starts at View 1. All eight successful
scan captures advance one step each, and the next round starts at View 1 again.
This is capture guidance, not proof that a complete 360-degree reconstruction
or adequate geometric coverage has been obtained.

## Runtime results

| Check | Observed result |
| --- | --- |
| Manually stopped video | Valid H.264 MP4, 720 × 734, 19.844511 seconds, 310,590 bytes, one video track and no audio track |
| Automatic duration limit on fixed APK | Stopped and finalized without tapping Stop; H.264 MP4, 720 × 734, 59.989111 seconds, 966,982 bytes, no audio track |
| Eight-view offline scan | Android reported no active default network; relay tunnel was removed; all eight views finalized locally and appeared as Waiting to upload |
| Offline force-stop/relaunch | All 11 originals retained with identical SHA-256 values; eight queued scans remained visible; relay still held only the two videos |
| Tunnel restored while Android remained offline | Queue remained waiting and relay count stayed at two |
| Android network restored | Existing workers automatically uploaded all eight scans without tapping Retry; relay held exactly ten captures: two videos and eight scan views |
| Integrity and UI acknowledgment | Every uploaded object's bytes and SHA-256 matched its retained native original; all 11 local entries showed Saved, including one photo from the earlier isolated smoke test |
| Spaces refresh | Opening Spaces showed ten captures and zero GPS coverage; the cached-data offline notice cleared after the directory refresh |

The older photo was retained on the emulator but belongs to the earlier
disposable workspace; it is not counted among this relay's ten captures.
The cached-data offline notice remained on Uploads until Spaces refreshed,
even though the upload badges had already changed to Saved. Automatic status
refresh while staying on Uploads remains a UI follow-up, not an upload failure.

Video original SHA-256 values:

- Manual stop: `394231033451a52b1f9afe511fea35ee2d3620de83b02a7f7b7a77fb6e9c473c`
- Automatic limit: `59f6b13d5b80e0cb5def355c5ba785fb98dc2e0978dc419c667e0c4e9699a013`

Local ignored evidence includes `output/playwright/android-scan-sequence.json`
and `android-capture-{before-restart,after-restart,tunnel-restored,recovered}.json`
in the same directory. Those reports retain the original checksum mapping.
Generated media, credentials, APKs, emulator state and disposable relay storage
are not committed.

## Build and remaining qualification

Both `assembleFakeDebug` and `assembleProbeDebug` pass. Unit tests pass for fake
(89) and probe (109), including the three new regression tests in each variant.
Both Android lint tasks pass; existing warnings remain documented.

This does not qualify physical camera/GPS/heading accuracy, real-world scan
geometry, permission revocation, picker/provider behavior, Doze/OEM scheduling,
large files, killing the process during capture, or interrupting an in-flight
upload. The restart here occurred after files finalized and before they could
upload. It also does not establish production HTTPS or multi-device acceptance.
See the [Android follow-ups](../atlas-android.md#still-required) and
[PR handoff](../atlas-pr-handoff-2026-09-08.md).
