# Orbit Repository Steward Receipt

## Header
- **Work Item**: `REPO-W2-001 — Bootstrap Orbit Repository`
- **From**: `Repository Steward`
- **To**: `Orbit PM Pair (Product Owner / Human PM & AI PM)`
- **Status**: `COMPLETE_WITH_NOTES`
- **Date**: `2026-08-18`
- **Milestone / Wave**: `M0-WAVE-02`
- **Repository Root**: [e:/Users/Louis/Documents/Orbit](file:///e:/Users/Louis/Documents/Orbit)
- **Primary Handoff Deliverable**: [HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md](file:///e:/Users/Louis/Documents/Orbit/HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md)

---

## 1. Repository Identification & Branch Registry

| Property | Value | Notes |
| :--- | :--- | :--- |
| **Repository Name** | `orbit` | Private-ready ambient companion repo |
| **Local File System URI** | [e:/Users/Louis/Documents/Orbit](file:///e:/Users/Louis/Documents/Orbit) | Canonical workspace root |
| **Default / Release Branch** | `main` | Production release baseline |
| **Integration Staging Branch** | `integration` | Gated integration staging branch |
| **Current Head Commit** | `d41f98f` | Synchronized across `main` and `integration` |
| **Working Tree Status** | Clean | Zero uncommitted or untracked changes |

---

## 2. Clickable Manifest of All Created Components

### Handoff & Governance Archive
- [HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md](file:///e:/Users/Louis/Documents/Orbit/HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md) — Authoritative Wave 02 PM handoff
- [handoffs/M0/HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md](file:///e:/Users/Louis/Documents/Orbit/handoffs/M0/HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md) — Archived Wave 02 handoff
- [handoffs/M0/HANDOFF_M0-WAVE-01_REPOSITORY-STEWARD_TO_PM.md](file:///e:/Users/Louis/Documents/Orbit/handoffs/M0/HANDOFF_M0-WAVE-01_REPOSITORY-STEWARD_TO_PM.md) — Archived Wave 01 handoff
- [handoffs/README.md](file:///e:/Users/Louis/Documents/Orbit/handoffs/README.md) — Handoff protocol directory guide

### CI & Build Configuration
- [.github/workflows/ci.yml](file:///e:/Users/Louis/Documents/Orbit/.github/workflows/ci.yml) — GitHub Actions CI workflow (Static analysis, unit tests, evals, Android assembleDebug)
- [.gitignore](file:///e:/Users/Louis/Documents/Orbit/.gitignore) — Comprehensive ignore rules with strict zero-secret blocking
- [settings.gradle.kts](file:///e:/Users/Louis/Documents/Orbit/settings.gradle.kts) — Multi-project settings (`:core`, `:android:app`, `:evals`)
- [build.gradle.kts](file:///e:/Users/Louis/Documents/Orbit/build.gradle.kts) — Root build script with provisional plugin declarations
- [gradle.properties](file:///e:/Users/Louis/Documents/Orbit/gradle.properties) — JVM args and AndroidX configuration
- [gradle/wrapper/gradle-wrapper.properties](file:///e:/Users/Louis/Documents/Orbit/gradle/wrapper/gradle-wrapper.properties) — Provisional Gradle 8.6 wrapper specification

### Modules & Domain Packages
- [core/build.gradle.kts](file:///e:/Users/Louis/Documents/Orbit/core/build.gradle.kts) — Pure Kotlin JVM domain module (Decoupled from Android SDK)
- [core/src/main/kotlin/org/orbit/core/](file:///e:/Users/Louis/Documents/Orbit/core/src/main/kotlin/org/orbit/core/) — Domain engine packages (`engine/`, `memory/`, `policy/`, `model/`)
- [android/build.gradle.kts](file:///e:/Users/Louis/Documents/Orbit/android/build.gradle.kts) — Android root module script
- [android/app/build.gradle.kts](file:///e:/Users/Louis/Documents/Orbit/android/app/build.gradle.kts) — Android app module with provisional SDK declarations
- [android/app/src/main/](file:///e:/Users/Louis/Documents/Orbit/android/app/src/main/) — Android source tree (`java/org/orbit/android/`, `res/`)
- [contracts/README.md](file:///e:/Users/Louis/Documents/Orbit/contracts/README.md) — Single-source-of-truth contracts specification
- [contracts/schemas/](file:///e:/Users/Louis/Documents/Orbit/contracts/schemas/) — Schema definitions directory
- [contracts/fixtures/](file:///e:/Users/Louis/Documents/Orbit/contracts/fixtures/) — Schema validation test fixtures directory
- [evals/README.md](file:///e:/Users/Louis/Documents/Orbit/evals/README.md) — Behavioral evaluation harness overview
- [evals/build.gradle.kts](file:///e:/Users/Louis/Documents/Orbit/evals/build.gradle.kts) — Evaluation module build script
- [evals/scenarios/](file:///e:/Users/Louis/Documents/Orbit/evals/scenarios/) — Golden scenario vectors (INTERVENE / SILENT / BLOCKED)
- [evals/harness/](file:///e:/Users/Louis/Documents/Orbit/evals/harness/) — Scenario test runner package
- [evals/reports/](file:///e:/Users/Louis/Documents/Orbit/evals/reports/) — Scenario run results directory
- [spikes/README.md](file:///e:/Users/Louis/Documents/Orbit/spikes/README.md) — Technical spike sandbox rules (Strictly isolated from production)
- [docs/README.md](file:///e:/Users/Louis/Documents/Orbit/docs/README.md) — Documentation index ([adr/](file:///e:/Users/Louis/Documents/Orbit/docs/adr/), [specs/](file:///e:/Users/Louis/Documents/Orbit/docs/specs/), [security/](file:///e:/Users/Louis/Documents/Orbit/docs/security/))

### Tooling & Automation
- [tooling/scripts/validate-schemas.sh](file:///e:/Users/Louis/Documents/Orbit/tooling/scripts/validate-schemas.sh) — Schema syntax verification script
- [tooling/scripts/run-evals.sh](file:///e:/Users/Louis/Documents/Orbit/tooling/scripts/run-evals.sh) — Scenario eval suite execution script
- [tooling/scripts/verify-locks.sh](file:///e:/Users/Louis/Documents/Orbit/tooling/scripts/verify-locks.sh) — Dependency lockfile verification script
- [tooling/git-hooks/pre-commit.sample](file:///e:/Users/Louis/Documents/Orbit/tooling/git-hooks/pre-commit.sample) — Pre-commit secret scanning hook

---

## 3. Toolchain & Provisional Settings Register

| Parameter | Setting | Status | Rationale |
| :--- | :--- | :--- | :--- |
| `minSdk` | `29` | **PROVISIONAL** | Subject to M0 Android capability spike on notifications/alarms |
| `compileSdk` | `34` | **PROVISIONAL** | Subject to Mobile TL recommendation |
| `targetSdk` | `34` | **PROVISIONAL** | Subject to Mobile TL recommendation |
| AGP Version | `8.3.2` | **PROVISIONAL** | Subject to Gradle toolchain compatibility |
| Kotlin Version | `1.9.23` | **PROVISIONAL** | Stable Kotlin compiler baseline |
| Gradle Version | `8.6` | **PROVISIONAL** | Subject to AGP compatibility |
| Java Version | `JDK 17` | **PROVISIONAL** | Aligned with standard Android Studio Iguana/Jellyfish baseline |
| `backend/` | *Omitted* | **ENFORCED** | Not required for on-device ambient V0 thesis |

---

## 4. Complete Git Commit History

```text
* d41f98f (HEAD -> main, integration) docs(handoff): record REPO-W2-001 Repository Steward handoff to PM
* 72391f7 feat(repo): bootstrap Orbit repository skeleton, provisional Gradle build, and CI workflows
* 47db871 chore(repo): initialize Orbit M0 repository layout and handoff governance
```

---

## 5. Next Recommended Action

**Action**: Authorize Architecture TL, Mobile Worker, Product/Research TL, and QA/Safety TL to begin branching from `integration` and committing their Wave 02 deliverables.  
**Proposed Owner**: `Orbit PM Pair` (Product Owner / Human PM & AI PM).
