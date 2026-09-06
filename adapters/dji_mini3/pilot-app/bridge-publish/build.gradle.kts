// Pure Kotlin/JVM: the WHIP publish path's testable half (Phase F, issue #51). The WHIP HTTP
// client, SDP munging, codec gate, publish state machine with bounded backoff, and metrics
// aggregation live here so they run against MockWebServer on a plain JDK 17; the libwebrtc
// PeerConnection, the capturers, and the DJI frame source stay in the Android app module.
plugins {
    alias(libs.plugins.kotlin.jvm)
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
        allWarningsAsErrors.set(true)
    }
}

dependencies {
    api(project(":bridge-core"))
    api(libs.coroutines.core)
    api(libs.okhttp)
    testImplementation(libs.junit.jupiter)
    testImplementation(libs.mockwebserver)
    testRuntimeOnly(libs.junit.platform.launcher)
}

tasks.test {
    useJUnitPlatform()
    testLogging {
        events("failed")
        exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL
    }
}
