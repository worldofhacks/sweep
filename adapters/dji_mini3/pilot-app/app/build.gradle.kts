plugins {
    id("com.android.application")
    kotlin("android")
}

android {
    namespace = "org.worldofhacks.sweep.dji"
    compileSdk = 35

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "org.worldofhacks.sweep.dji"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        manifestPlaceholders["DJI_APP_KEY"] = providers.gradleProperty("DJI_APP_KEY").orElse("").get()
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    implementation(project(":command-admission"))
    implementation("com.dji:dji-sdk-v5-aircraft:${property("MSDK_VERSION")}")
    compileOnly("com.dji:dji-sdk-v5-aircraft-provided:${property("MSDK_VERSION")}")
    runtimeOnly("com.dji:dji-sdk-v5-networkImp:${property("MSDK_VERSION")}")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
