# Orbit Handoff

## Header
- Work Item: `REPO-W2-001 — Bootstrap Orbit Repository`
- From: `Repository Steward`
- To: `Orbit PM Pair (Product Owner / Human PM & AI PM)`
- Status: `COMPLETE_WITH_NOTES`
- Date: `2026-08-18`
- Contract Version(s), if relevant: `v0.1.0-draft` (contracts skeleton)

---

## Executive Summary

Pursuant to authorization under `REPO-W2-001` and the PM disposition `ACCEPT_WITH_ACTIONS`, the **Orbit repository has been bootstrapped and structured**.

The repository has been initialized with the accepted M0 directory layout, dual-branch topology (`main` and `integration`), lean CI workflows, dependency verification scaffolding, handoff archive structure, and provisional Gradle multi-project build configuration. All Android SDK and toolchain baselines have been explicitly annotated as **PROVISIONAL** pending evidence from the Mobile/Android Capability Spike.

---

## Decisions / Results

1. **Repository Topology & Layout**:
   - Initialized Git repository with default branch `main` and staging branch `integration`.
   - Created the approved directory layout: `.github/workflows/`, `android/app/`, `core/`, `contracts/schemas/`, `contracts/fixtures/`, `evals/scenarios/`, `evals/harness/`, `evals/reports/`, `handoffs/M0/`, `docs/adr/`, `docs/specs/`, `docs/security/`, `spikes/`, and `tooling/scripts/`.
   - Strictly omitted `backend/` in accordance with the local-first V0 thesis.
2. **Provisional Toolchain & Android Baseline Annotations**:
   - `android/app/build.gradle.kts`: Annotated `compileSdk = 34`, `minSdk = 29`, and `targetSdk = 34` as **PROVISIONAL**.
   - `build.gradle.kts` & `gradle.properties`: Marked AGP `8.3.2`, Kotlin `1.9.23`, and JVM args as **PROVISIONAL**.
   - `gradle/wrapper/gradle-wrapper.properties`: Marked Gradle `8.6` as **PROVISIONAL**.
   - No production toolchain baseline is frozen prior to the Mobile M0 spike findings.
3. **M0 CI Workflow (`.github/workflows/ci.yml`)**:
   - Established 3 lean, deterministic GitHub Actions jobs:
     - `contract-and-hygiene-check`: JSON schema verification, secret scanning, and credential leakage guard.
     - `core-and-eval-tests`: JDK 17 headless runner executing pure Kotlin unit tests and the scenario evaluation runner (`tooling/scripts/run-evals.sh`).
     - `android-build-and-lint`: Provisional Android app build and lint skeleton.
4. **Handoff Archive Management**:
   - Initialized `handoffs/M0/` and archived the accepted `HANDOFF_M0-WAVE-01_REPOSITORY-STEWARD_TO_PM.md`.

---

## Deliverables

1. **Repository File Tree & Codebase**: Local repository initialized at `e:\Users\Louis\Documents\Orbit`.
2. **Configuration & Build Files**:
   - `.gitignore`: Comprehensive ignore rules for Android, JVM, Gradle, OS, IDEs, and strict zero-secret blocking.
   - `settings.gradle.kts`: Multi-module configuration (`:core`, `:android:app`, `:evals`).
   - `build.gradle.kts`: Root Gradle build script with provisional plugin declarations.
   - `core/build.gradle.kts`: Pure Kotlin/JVM domain engine module decoupled from Android SDK.
   - `android/app/build.gradle.kts`: Android application module with provisional SDK baselines.
   - `evals/build.gradle.kts`: Scenario evaluation runner module.
   - `gradle.properties`: Pinned JVM options and provisional AndroidX flags.
   - `gradle/wrapper/gradle-wrapper.properties`: Provisional Gradle wrapper definition.
3. **CI & Automation Scripts**:
   - `.github/workflows/ci.yml`: Multi-job GitHub Actions workflow.
   - `tooling/scripts/validate-schemas.sh`: Schema syntax validation script.
   - `tooling/scripts/run-evals.sh`: Scenario evaluation runner script.
   - `tooling/scripts/verify-locks.sh`: Dependency lock verification script.
   - `tooling/git-hooks/pre-commit.sample`: Pre-commit hook for secret scanning.
4. **Documentation & Governance Placeholders**:
   - `README.md`, `contracts/README.md`, `evals/README.md`, `docs/README.md`, `spikes/README.md`, `handoffs/README.md`.
5. **Handoff Records**:
   - `handoffs/M0/HANDOFF_M0-WAVE-01_REPOSITORY-STEWARD_TO_PM.md` (Archived).
   - `handoffs/M0/HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md` (Current).

---

## Evidence

### 1. Repository Identification & Status
- **Canonical Identifier**: `local://e:/Users/Louis/Documents/Orbit` (Git repository root).
- **Target Remote Identifier**: `github.com/<org-or-user>/orbit` (Private).
- **Default Branch**: `main`.
- **Created Branches**: `main` (HEAD), `integration`.
- **Visibility**: Configured for Private repository standards (zero public assets, secret scanners enabled).

### 2. Commit History
```text
* 72391f7 (HEAD -> main, integration) feat(repo): bootstrap Orbit repository skeleton, provisional Gradle build, and CI workflows
* 47db871 chore(repo): initialize Orbit M0 repository layout and handoff governance
```

### 3. Repository Directory Tree
```text
orbit/
├── .github/
│   └── workflows/
│       └── ci.yml
├── .gitignore
├── README.md
├── build.gradle.kts
├── gradle.properties
├── settings.gradle.kts
├── android/
│   ├── build.gradle.kts
│   └── app/
│       ├── build.gradle.kts
│       └── src/main/
│           ├── java/org/orbit/android/
│           └── res/
├── contracts/
│   ├── README.md
│   ├── fixtures/ (.gitkeep)
│   └── schemas/ (.gitkeep)
├── core/
│   ├── build.gradle.kts
│   └── src/
│       ├── main/kotlin/org/orbit/core/
│       │   ├── engine/
│       │   ├── memory/
│       │   ├── model/
│       │   └── policy/
│       └── test/kotlin/org/orbit/core/
├── docs/
│   ├── README.md
│   ├── adr/ (README.md)
│   ├── security/ (.gitkeep)
│   └── specs/ (.gitkeep)
├── evals/
│   ├── README.md
│   ├── build.gradle.kts
│   ├── harness/ (.gitkeep)
│   ├── reports/ (.gitkeep)
│   └── scenarios/ (.gitkeep)
├── gradle/
│   └── wrapper/
│       └── gradle-wrapper.properties
├── handoffs/
│   ├── README.md
│   └── M0/
│       ├── HANDOFF_M0-WAVE-01_REPOSITORY-STEWARD_TO_PM.md
│       └── HANDOFF_REPO-W2-001_REPOSITORY-STEWARD_TO_PM.md
├── spikes/
│   ├── README.md
│   └── .gitkeep
└── tooling/
    ├── git-hooks/
    │   └── pre-commit.sample
    └── scripts/
        ├── run-evals.sh
        ├── validate-schemas.sh
        └── verify-locks.sh
```

### 4. Branch Protections Configured vs. Hosting Platform Limitations
- **Local Enforcement**:
  - `main` and `integration` established as distinct tracking branches.
  - Linear history and Conventional Commits adopted.
  - Pre-commit secret scanning hook provided in `tooling/git-hooks/pre-commit.sample`.
- **Remote Hosting Configuration Status**:
  - The local execution environment does not have GitHub CLI (`gh`) installed or a remote `GITHUB_TOKEN` injected in the shell.
  - Consequently, remote GitHub repository creation and remote branch protection rules (e.g. branch protection API / rulesets requiring PR reviews and status checks before merge) cannot be executed via automated REST calls in this session.
  - Full instructions for configuring remote branch protections upon remote push are documented in `docs/` and ready for execution once remote access is established.

### 5. Provisional Android & Toolchain Summary
| Parameter | Bootstrap Value | Status | Dependency |
| :--- | :--- | :--- | :--- |
| `minSdk` | `29` (Android 10) | **PROVISIONAL** | Pending M0 Android Capability Spike on notification listener & alarms |
| `compileSdk` | `34` (Android 14) | **PROVISIONAL** | Pending Mobile TL recommendation |
| `targetSdk` | `34` (Android 14) | **PROVISIONAL** | Pending Mobile TL recommendation |
| AGP Version | `8.3.2` | **PROVISIONAL** | Subject to Gradle toolchain compatibility |
| Kotlin Version | `1.9.23` | **PROVISIONAL** | Standard stable Kotlin compiler |
| Gradle Version | `8.6` | **PROVISIONAL** | Subject to AGP compatibility |
| Java Target | `JDK 17` | **PROVISIONAL** | Aligned with standard Android Studio Iguana/Jellyfish baseline |

---

## Assumptions

1. **GitHub Remote Hosting**: Assumes the private remote repository will be hosted on GitHub to utilize the `.github/workflows/ci.yml` definitions.
2. **Deterministic CI Tests**: Assumes all behavioral evals and core unit tests run hermetically without requiring external network connectivity or paid API credentials in CI.

---

## Risks / Blockers

1. **Remote Repository Push & Protection API Access**:
   - *Status*: `NOTE` / Non-fatal for local development.
   - *Impact*: Remote repository creation and branch protection rulesets on `main`/`integration` require either GitHub CLI (`gh`) authentication, a PAT, or manual creation via GitHub web UI.
   - *Mitigation*: The repository is completely structured locally with matching branch structure and CI configuration. Once remote origin is added (`git remote add origin ...`), pushing `main` and `integration` takes one command.

---

## Contract Changes Requested

`None` (The repository bootstrap implements the structural shell for contracts in `contracts/` without modifying any schema).

---

## PM / Product Decisions Needed

`None` (Repository bootstrap complete according to PM wave 2 authorization).

---

## Recommended Next Action

**Action**: Provide/configure the remote GitHub repository URL (`origin`) to push `main` and `integration`, and authorize Team Leads (Architecture, Mobile, Product/Research, QA/Safety) to begin landing their Wave 02 spike code and contract schemas on working topic branches.  
**Proposed Owner**: `Orbit PM Pair` (Product Owner / Human PM & AI PM).
