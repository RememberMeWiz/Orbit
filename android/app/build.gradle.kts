// Orbit Android App Module
// ============================================================================
// PROVISIONAL SDK NOTICE:
// compileSdk (34), minSdk (29), targetSdk (34) below are temporary bootstrap
// placeholders and are explicitly PROVISIONAL pending evidence from the M0
// Android/Mobile Capability Spike.
// ============================================================================

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "org.orbit.android"
    
    // PROVISIONAL: Pending Android spike results
    compileSdk = 34

    defaultConfig {
        applicationId = "org.orbit.android"
        // PROVISIONAL: Minimum SDK baseline subject to API spike validation
        minSdk = 29
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0-alpha.0-provisional"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(project(":core"))

    // AndroidX & UI Placeholders (PROVISIONAL)
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")

    testImplementation("junit:junit:4.13.2")
}
