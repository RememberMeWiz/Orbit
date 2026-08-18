// Orbit - Root Settings Gradle
// Note: Multi-project layout for Orbit.
// Toolchain and plugin choices are PROVISIONAL pending Mobile/Android spike results.

rootProject.name = "orbit"

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

// Module inclusions
include(":core")
include(":android:app")
include(":evals")
