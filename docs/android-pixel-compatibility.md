# Android 16 KB native-library compatibility

The integrated pilot app uses `io.getstream:stream-webrtc-android:1.3.10`.
The previous 1.1.1 ARM64 `libjingle_peerconnection_so.so` had three ELF LOAD
segments with 4 KB alignment. Changing APK ZIP packaging alone cannot repair
that ELF layout. This selectively restores the compatibility fix from PR #268
while retaining the current aircraft authority, signed height-policy and landing
recovery implementation.

WebRTC 1.3.10 also requires an explicit scalability-mode list in `VideoCodecInfo`.
The H.264 passthrough factory supplies an empty list; its codec parameters remain
unchanged. DJI 5.18.0, compile/target SDK 35 and the existing compressed native
library packaging remain unchanged. No page-size warning suppression or runtime
compatibility override is configured.

From `adapters/dji_mini3/pilot-app`, build and check both native-library sets:

```sh
./gradlew --no-daemon -PsweepSupervisedVertical=false :app:assembleProbeDebug :app:assembleFakeDebug
python3 tools/check_apk_alignment.py app/build/outputs/apk/probe/debug/app-probe-debug.apk
python3 tools/check_apk_alignment.py app/build/outputs/apk/fake/debug/app-fake-debug.apk
"$ANDROID_HOME/build-tools/35.0.0/zipalign" -c -P 16 4 app/build/outputs/apk/probe/debug/app-probe-debug.apk
"$ANDROID_HOME/build-tools/35.0.0/zipalign" -c -P 16 4 app/build/outputs/apk/fake/debug/app-fake-debug.apk
```

Use an installed Android Build-Tools version of at least 35.0.0 for `zipalign`.
The explicit `sweepSupervisedVertical=false` selects the ordinary physical probe
build. A deployment using the separate supervised vertical profile must build
with its documented selector and check that resulting APK as well. The fake
flavor is only a software fixture and must not be used for physical testing.

The checker inspects every packaged `arm64-v8a` and `x86_64` shared library. Each
ELF LOAD segment needs a power-of-two alignment of at least 16 KB and congruent
file offsets and virtual addresses. Uncompressed native libraries also need
16 KB-aligned ZIP payloads. Compressed libraries still require compatible ELF
segments. Malformed ELF headers and APKs containing no 64-bit native libraries
fail the check. Tests use generated binary-layout fixtures, without loading or
executing native code.

The Android CI job assembles both flavors with an empty DJI registration key,
runs the checker and Android SDK `zipalign`, and does not upload APKs. Keep locally
configured APKs private: their manifest may contain a DJI registration key.

Local reconciliation checks on September 7, 2026 passed: 20 checker regression
tests, 297 JVM tests and 98 Android unit tests. The rebuilt probe APK passed for
all 64 native libraries and the fake APK for both native libraries. Android SDK
Build-Tools 36.0.0 independently passed ZIP alignment for both APKs. The original
probe APK failed on the three 4 KB WebRTC LOAD segments before this change.

These checks establish binary layout only. Android's [16 KB page-size
guidance](https://developer.android.com/guide/practices/page-sizes) also calls for
runtime testing on a 16 KB environment. This reconciliation does not establish
that runtime result, phone/RC/aircraft connection, video publication or flight
acceptance. The historical PR #268 phone result is not acceptance evidence for
the current integrated APK.
