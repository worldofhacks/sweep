# Pixel Android page-size compatibility

The Pixel 8 running Android 16 reported `PageSizeMismatchDialog` when launching
the Sweep probe app. Inspection of the installed APK identified one incompatible
native library: `libjingle_peerconnection_so.so` from
`io.getstream:stream-webrtc-android:1.1.1`. Its three ELF LOAD segments used 4 KB
alignment. The other 63 ARM64 libraries, including DJI 5.18, were 16 KB aligned.
APK ZIP alignment already passed; changing ZIP packaging would not fix this ELF.

The fix upgrades the same WebRTC dependency to 1.3.10, whose ARM64 LOAD segments
are 16 KB aligned. Its `VideoCodecInfo` constructor also requires an explicit
empty scalability-mode list. Existing H.264 parameters, DJI 5.18, and
compile/target SDK 35 remain unchanged. No warning-suppression or Android
compatibility override is set.

From `adapters/dji_mini3/pilot-app`, check each assembled flavor:

```sh
python3 tools/check_apk_alignment.py app/build/outputs/apk/probe/debug/app-probe-debug.apk
python3 tools/check_apk_alignment.py app/build/outputs/apk/fake/debug/app-fake-debug.apk
```

The guard checks every packaged 64-bit native library's ELF LOAD alignment and
offset/address congruence. Uncompressed native libraries must also have 16 KB
aligned ZIP payloads. Compressed native libraries still need compatible ELF
segments. An APK containing no 64-bit native libraries fails the check.

Validation on September 6, 2026:

- Both flavors built and 72 focused Android/publisher JVM tests passed.
- The old probe APK failed on the three WebRTC LOAD segments. The replacement
  passed for all 64 probe libraries and both fake-flavor libraries, including the
  Android SDK's independent ZIP alignment check.
- The replacement probe APK was installed on the Pixel 8 without clearing its
  encrypted D-02 configuration. The installed APK hash matched the build.
- Android's reported `pageSizeCompat` changed from `4` to `0`. The relaunch
  produced no new `PageSizeMismatchDialog` or native/runtime fatal error in the
  captured logs.

The tested phone currently reports a 4096-byte kernel page size. These findings
prove removal of its warning and compatible binary layout; they do not claim a
16 KB-kernel runtime test. DJI registration, USB product connection, aircraft
telemetry, and physical flight remain separate checks. Registration alone does
not establish an RC/aircraft connection.
