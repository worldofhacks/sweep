pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "sweep-pilot-app"

// The pure-JVM modules build anywhere a JDK exists (the bridge-jvm CI job has no Android SDK).
include(":bridge-core")
include(":bench")
include(":bridge-node")

// The Android module is only configured when an SDK location can be resolved, so
// `./gradlew :bridge-core:test :bench:test` never trips over "SDK location not found".
val localProperties = java.util.Properties().apply {
    val file = rootDir.resolve("local.properties")
    if (file.isFile) file.inputStream().use { load(it) }
}
val sdkDir = localProperties.getProperty("sdk.dir")
    ?: System.getenv("ANDROID_HOME")
    ?: System.getenv("ANDROID_SDK_ROOT")
if (sdkDir != null && File(sdkDir).isDirectory) {
    include(":app")
} else {
    logger.lifecycle("Android SDK not found (local.properties sdk.dir, ANDROID_HOME, ANDROID_SDK_ROOT); skipping :app")
}
