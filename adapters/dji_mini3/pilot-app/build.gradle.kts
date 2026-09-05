// Root build: plugin versions live in gradle/libs.versions.toml. AGP 9 compiles Kotlin
// itself (built-in Kotlin), so the Android module applies no kotlin-android plugin.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.compose) apply false
}
