// Orbit Core Module - Pure JVM Kotlin
// Decoupled from Android SDK to allow fast, deterministic execution of
// state machine, memory models, deduplication, and policy logic.

plugins {
    kotlin("jvm")
}

dependencies {
    // Pure Kotlin stdlib
    implementation(kotlin("stdlib"))

    // Testing
    testImplementation(kotlin("test"))
}

tasks.test {
    useJUnitPlatform()
}
