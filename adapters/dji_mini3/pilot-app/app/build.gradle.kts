// Android pilot app. The MSDK wiring below (key resolution, probe flavor, arm64 filter,
// native-library packaging, DJI dependency scopes) is ported from techmexdev/drone-maps
// app/build.gradle.kts. Atlas uses WorkManager for phone capture uploads, independently of flight.
plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

// DJI key: Gradle property DJI_API_KEY (~/.gradle/gradle.properties), then the DJI_APP_KEY
// environment variable, else empty so a keyless build still assembles.
val djiAppKey: String = providers.gradleProperty("DJI_API_KEY").orNull
    ?: providers.environmentVariable("DJI_APP_KEY").orNull
    ?: ""
val supervisedVertical: Boolean = providers.gradleProperty("sweepSupervisedVertical").orNull == "true"

android {
    namespace = "org.worldofhacks.sweep.bridge"
    compileSdk = 35 // DJI MSDK 5.18's supported maximum

    defaultConfig {
        applicationId = "org.worldofhacks.sweep.bridge"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        manifestPlaceholders["DJI_API_KEY"] = djiAppKey
        buildConfigField("boolean", "SUPERVISED_VERTICAL", supervisedVertical.toString())
    }

    flavorDimensions += "aircraft"
    productFlavors {
        create("fake") {
            dimension = "aircraft"
            buildConfigField("String", "AIRCRAFT", "\"fake\"")
            ndk {
                // The WebRTC build ships four ABIs; the pinned phone is arm64 (Phase F).
                abiFilters += "arm64-v8a"
            }
        }
        create("probe") {
            dimension = "aircraft"
            buildConfigField("String", "AIRCRAFT", "\"dji-probe\"")
            ndk {
                // DJI MSDK v5 ships arm64 native libraries only.
                abiFilters += "arm64-v8a"
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    buildFeatures {
        buildConfig = true
        compose = true
    }
    testOptions.unitTests.isIncludeAndroidResources = true

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
        jniLibs.pickFirsts += "lib/arm64-v8a/libc++_shared.so"
        jniLibs.useLegacyPackaging = true
        jniLibs.keepDebugSymbols += setOf(
            "**/libconstants.so",
            "**/libdji_innertools.so",
            "**/libdjibase.so",
            "**/libDJICSDKCommon.so",
            "**/libDJIFlySafeCore-CSDK.so",
            "**/libdjifs_jni-CSDK.so",
            "**/libDJIRegister.so",
            "**/libdjisdk_jni.so",
        )
    }
}

dependencies {
    implementation(project(":bridge-core"))
    implementation(project(":bridge-node"))
    implementation(project(":bridge-publish"))
    implementation(project(":bench"))

    val composeBom = platform(libs.compose.bom)
    implementation(composeBom)
    implementation(libs.activity.compose)
    implementation(libs.compose.foundation)
    implementation(libs.compose.material3)
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.core.ktx)
    implementation(libs.coroutines.android)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.security.crypto)
    implementation(libs.camera.camera2)
    implementation(libs.camera.lifecycle)
    implementation(libs.camera.view)
    implementation(libs.camera.video)
    implementation(libs.work.runtime)
    implementation(libs.webkit)
    // WHIP publish path (Phase F): libwebrtc prebuilt used by the vendored WildBridge package.
    implementation(libs.stream.webrtc)
    debugImplementation(libs.compose.ui.tooling)

    "probeImplementation"(libs.dji.aircraft)
    "probeCompileOnly"(libs.dji.aircraft.provided)
    "probeRuntimeOnly"(libs.dji.network)

    testImplementation(libs.junit.jupiter)
    testRuntimeOnly(libs.junit.platform.launcher)
    testImplementation(libs.robolectric)
    testImplementation(libs.work.testing)
    testImplementation(libs.mockwebserver)
    testRuntimeOnly(libs.junit.vintage)
}

tasks.withType<Test>().configureEach {
    useJUnitPlatform()
    jvmArgs("--add-opens=java.base/java.lang=ALL-UNNAMED", "--add-opens=java.base/java.util=ALL-UNNAMED",
        "--add-opens=java.base/java.io=ALL-UNNAMED", "--add-opens=java.base/java.net=ALL-UNNAMED",
        "--add-opens=java.base/java.security=ALL-UNNAMED", "--add-opens=java.base/java.text=ALL-UNNAMED",
        "--add-opens=java.base/jdk.internal.access=ALL-UNNAMED", "--add-opens=java.desktop/java.awt.font=ALL-UNNAMED")
}

// Bundle the existing Atlas source, not a remote page or a copy of its components.
// A clean checkout needs `pnpm --dir console install --frozen-lockfile` before the Android build.
val atlasConsole = rootProject.projectDir.resolve("../../../console")
val buildAtlasUi = tasks.register<Exec>("buildAtlasUi") {
    workingDir(atlasConsole)
    commandLine("pnpm", "build:android")
    inputs.dir(atlasConsole.resolve("src"))
    inputs.files("${atlasConsole}/atlas-native.html", "${atlasConsole}/vite.native.config.ts",
        "${atlasConsole}/package.json", "${atlasConsole}/pnpm-lock.yaml")
    outputs.dir(atlasConsole.resolve("dist-android"))
}
abstract class AtlasAssetsTask : DefaultTask() {
    @get:InputDirectory abstract val webBundle: DirectoryProperty
    @get:OutputDirectory abstract val outputDirectory: DirectoryProperty
    @get:Inject abstract val fileOperations: FileSystemOperations
    @TaskAction fun bundle() {
        fileOperations.sync { from(webBundle); into(outputDirectory.dir("atlas")) }
    }
}
val bundleAtlasUi = tasks.register<AtlasAssetsTask>("bundleAtlasUi") {
    dependsOn(buildAtlasUi)
    webBundle.set(atlasConsole.resolve("dist-android"))
    outputDirectory.set(layout.buildDirectory.dir("generated/atlasAssets"))
}
androidComponents.onVariants { variant ->
    variant.sources.assets?.addGeneratedSourceDirectory(bundleAtlasUi, AtlasAssetsTask::outputDirectory)
}
