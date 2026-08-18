// Orbit Evaluation Module
// Runs scenario vectors and policy evaluations against :core logic.

plugins {
    kotlin("jvm")
}

dependencies {
    implementation(project(":core"))
    implementation(kotlin("stdlib"))

    testImplementation(kotlin("test"))
}

tasks.test {
    useJUnitPlatform()
}
