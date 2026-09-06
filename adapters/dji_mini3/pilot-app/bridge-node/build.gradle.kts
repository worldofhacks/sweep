// Pure Kotlin/JVM: the node's relay link (OkHttp WebSocket client speaking the node protocol
// in relay/README.md) and the aircraft-facing interfaces the Android flavors implement.
// No Android types, so the bridge-jvm CI job runs these tests against a stub relay on a
// plain JDK 17.
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
