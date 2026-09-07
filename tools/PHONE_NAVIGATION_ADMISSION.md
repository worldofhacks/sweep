# Android navigation admission interop

Run this opt-in check from the repository root after setting the Android SDK and JDK locations:

```bash
JAVA_HOME=/tmp/sweep-build-jdk \
PATH=/tmp/sweep-build-jdk/bin:$PATH \
ANDROID_HOME=/var/tmp/gauntlet/sweep-android-sdk \
uv run python tools/check_phone_navigation_admission_interop.py \
  --android-project /path/to/pilot-app
```

The check builds a measured Python deployment fixture in a temporary directory, exports its six Android evidence files, and runs the real Kotlin `NavigationAdmissionFile` parser and signature verifier. It creates a temporary Kotlin test in the Android project and removes it after Gradle finishes.
