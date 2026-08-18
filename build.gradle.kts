// Orbit - Root Build Script
// ============================================================================
// PROVISIONAL CONFIGURATION NOTICE:
// Android Gradle Plugin (AGP), Kotlin versions, SDK baselines, and toolchains
// specified below are temporary bootstrap placeholders and are explicitly
// PROVISIONAL pending evidence from the M0 Android/Mobile Capability Spike.
// ============================================================================

plugins {
    // PROVISIONAL: Subject to Android/Mobile M0 spike recommendations
    id("com.android.application") version "8.3.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.23" apply false
    id("org.jetbrains.kotlin.jvm") version "1.9.23" apply false
}

allprojects {
    group = "org.orbit"
    version = "0.1.0-alpha.0-provisional"
}
