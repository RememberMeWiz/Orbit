# Orbit Handoff

## Header
- Work Item: `M0-WAVE-01: REPO-001, REPO-002, REPO-003, REPO-004`
- From: `Repository Steward`
- To: `Orbit PM Pair (Product Owner / Human PM & AI PM)`
- Status: `COMPLETE`
- Date: `2026-08-17`
- Contract Version(s), if relevant: `v0.1.0-draft` (contracts skeleton)

---

## Executive Summary

As the Repository Steward for Orbit, I have established the baseline repository governance, branch integration policies, minimum M0 continuous integration (CI) architecture, reproducibility standards, and handoff artifact management rules for M0-WAVE-01.

Key outcomes:
1. **Lean Repository Layout (REPO-001)**: Refined the mono-repo layout to strictly adhere to the Android-first local-first V0 thesis. Deferred a dedicated `backend/` service directory to prevent premature overhead while establishing clear homes for `contracts/`, `core/` (pure Kotlin domain engine), `android/`, `evals/`, `docs/`, `tooling/`, and `handoffs/`.
2. **Gated Integration Flow (REPO-002)**: Formalized a two-tier protected branch model (`main` and `integration`) with explicit promotion gates. Direct commits to protected branches are disabled. Promotion requires TL code review, mandatory QA approval for behavioral/permission/contract changes, green CI, and Steward merge sign-off.
3. **M0 CI & Reproducibility Skeleton (REPO-003)**: Designed a fast, deterministic CI pipeline covering schema validation, core unit tests, Android debug compilation, static analysis (ktlint/detekt), and scenario evaluation execution, backed by Gradle dependency locking and hermetic toolchains (JDK 17).
4. **Handoff Artifact Management (REPO-004)**: Standardized one-file handoffs (`.md` or `.zip` containing `HANDOFF.md` + `artifacts/`), establishing an immutable audit record in `handoffs/M<milestone>/`.

---

## Decisions / Results

### 1. REPO-001: Initial Repository Layout & Structure

We adopt a lightweight mono-repo optimized for Android-first development and decoupled evaluation:

```text
orbit/
├── .github/
│   └── workflows/              # CI workflow definitions (ci.yml, eval.yml)
├── android/                    # Android application module (UI, Services, WorkManager, Room, OS listeners)
│   ├── app/
│   │   ├── src/main/
│   │   │   ├── java/org/orbit/android/
│   │   │   └── res/
│   │   └── build.gradle.kts
│   └── build.gradle.kts
├── core/                       # Pure Kotlin/JVM domain engine (Decoupled from Android SDK)
│   ├── src/
│   │   ├── main/kotlin/org/orbit/core/
│   │   │   ├── engine/         # State machine, event queue, deterministic rule engine
│   │   │   ├── memory/         # Memory models, deduplication, provenance tracking
│   │   │   ├── policy/         # Intervention policy, permission guards, silence evaluation
│   │   │   └── model/          # Model client interfaces, prompt serialization, response parser
│   │   └── test/kotlin/org/orbit/core/
│   └── build.gradle.kts
├── contracts/                  # Shared technical contracts (Single Source of Truth)
│   ├── schemas/                # JSON Schema / Protobuf definitions (v0.1)
│   │   ├── event.schema.json
│   │   ├── memory.schema.json
│   │   ├── world_state.schema.json
│   │   ├── candidate_intervention.schema.json
│   │   ├── action.schema.json
│   │   └── permission.schema.json
│   ├── fixtures/               # Golden test vectors matching schema versions
│   └── README.md
├── evals/                      # Behavioral evaluation harness & scenario vectors
│   ├── scenarios/              # Golden scenario suite (JSON/YAML) (40-60 scenarios)
│   ├── harness/                # Scenario test runner (invokes core policy/engine)
│   └── reports/                # Evaluation run outputs and regression diffs
├── handoffs/                   # Accepted formal handoff archive
│   ├── M0/
│   └── M1/
├── docs/                       # Architecture, research, QA, and governance documentation
│   ├── adr/                    # Architecture Decision Records (ADR-0001-*)
│   ├── specs/                  # Technical and behavioural specifications
│   └── security/               # Threat models & privacy boundaries
├── spikes/                     # Isolated technical experiments (Throwaway / Non-production)
│   └── README.md
├── tooling/                    # Automation, linters, pre-commit hooks, schema codegen
│   ├── scripts/
│   │   ├── validate-schemas.sh
│   │   ├── run-evals.sh
│   │   └── verify-locks.sh
│   └── git-hooks/
├── gradle/
│   └── wrapper/
├── build.gradle.kts            # Root build script
├── settings.gradle.kts
└── gradle.properties
```

#### Layout Rationale:
- **Omission of `backend/`**: In accordance with the M0 constraints, Orbit V0 is an on-device ambient companion connecting directly to model APIs via a secure client interface or mock provider. No standalone backend service is justified for M0. If cloud synchronization or a custom gateway is justified in M1+, a `backend/` module can be created via an approved ADR.
- **Decoupling `core/` from `android/`**: `core/` contains no Android framework dependencies (`android.*`). This enables millisecond-level local unit testing and scenario evaluation in CI without requiring an Android emulator or Robolectric overhead.
- **Dedicated `contracts/`**: Enforces contract immutability and versioning. Both `core/` and `android/` consume contracts defined here.
- **Dedicated `handoffs/`**: Preserves organizational memory and peer accountability in git history under `handoffs/M0/`.
- **Isolated `spikes/`**: Explicit directory for exploratory spikes (e.g., Android notification listener benchmarks) to guarantee experimental code never leaks into production modules.

---

### 2. REPO-002: Branch Governance and Integration Policy

#### Branch Topology
```text
┌────────────────────────────────────────────────────────┐
│ main (Production Release / Golden Stable)              │  ◄── Tagged: v0.1.0
└───────────────────────────▲────────────────────────────┘
                            │ (PM Release Decision + Full System QA)
┌───────────────────────────┴────────────────────────────┐
│ integration (Continuous Integration / Staging Gate)    │
└───────▲───────────────▲────────────────▲───────────────┘
        │               │                │
┌───────┴──────┐ ┌──────┴──────┐  ┌──────┴──────┐
│ mobile/*     │ │ agent/*     │  │ qa/*        │   (Working Topic Branches)
│ (Android TL/ │ │ (Core/      │  │ (Test/Eval  │
│  Workers)    │ │  Reasoning) │  │  Suites)    │
└──────────────┘ └─────────────┘  └─────────────┘
```

#### Governance Matrix
| Branch Pattern | Allowed Mergers | Required Reviewers | Required CI Checks | Protection Rules |
| :--- | :--- | :--- | :--- | :--- |
| `main` | **Repository Steward Only** | PM Pair (Human + AI PM) + QA Lead | Full Release Build + Full Scenario Eval + Security Scan | Force-push disabled, deletion disabled, linear history required. |
| `integration` | **Repository Steward Only** | Relevant Team Lead (Mobile/Arch) + QA Lead (if touching behavior/contracts) | Core Unit Tests + Schema Validation + Android Debug Build + Lints | Force-push disabled, deletion disabled, branch up-to-date required before merge. |
| Working (`mobile/*`, `agent/*`, `arch/*`, `qa/*`, `repo/*`) | Assigned Worker / TL | Peer / Self (for drafting) | Local pre-commit hooks | Ephemeral; deleted upon merge into `integration`. |
| `spikes/*` | Spike Author | Architecture TL (FYI) | Non-blocking | Strictly isolated; cannot merge to `integration` directly. |

#### Merge & Review Flow
1. **Branch Naming**: `<domain>/<work-item-id>-<short-description>`
   - Examples: `mobile/ARCH-002-notification-listener`, `agent/ARCH-001-event-schema`, `qa/QA-002-permission-boundary`, `repo/REPO-001-ci-setup`.
2. **Commit Expectations**: Conventional Commits standard:
   - `feat(core): implement event deduplication logic`
   - `fix(android): resolve notification channel permission crash`
   - `contract(event): bump schema_version to 0.1.1`
   - `test(evals): add golden scenarios for silent studying context`
   - `chore(deps): update gradle dependency locks`
3. **Contract Change Protection**: Any PR modifying files in `contracts/` or `core/model/` must have mandatory sign-off from **Architecture TL**.
4. **QA Protection**: Any PR changing behavior, intervention policy, permission checks, or scenario baselines requires **QA & Safety TL** approval.
5. **Merge Strategy**: Squash-and-merge or Rebase-and-merge to `integration` to maintain a clean linear git log.
6. **Rollback Policy**:
   - Immediate `git revert` PR to `integration` if regressions slip past CI.
   - Hotfix branches (`hotfix/*`) target `integration`, validated by CI, then fast-tracked to `main` with PM approval.
7. **Versioning & Tagging**:
   - Semantic Versioning: `v<MAJOR>.<MINOR>.<PATCH>[-<PRERELEASE>]`
   - M0 milestones tagged as `v0.0.1-m0.wave1`, `v0.0.1-m0.wave2`.
   - V0 releases on `main` tagged as `v0.1.0-alpha.1`, `v0.1.0`.

---

### 3. REPO-003: CI and Reproducibility Requirements

The M0 CI pipeline is designed to be lean, fast (< 4 minutes execution), and strictly deterministic.

#### Minimum CI Pipeline Jobs (GitHub Actions / Runner)
```text
  ┌────────────────────────────────────────────────────────┐
  │                   Trigger: PR / Push                   │
  └───────────────────────────┬────────────────────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ Job 1: Lint &   │  │ Job 2: Core JVM │  │ Job 3: Android  │
│ Contract Check  │  │ Unit Tests &    │  │ Assemble & Lint │
│ - JSON schema   │  │ Scenario Evals  │  │ - assembleDebug │
│   validation    │  │ - testCore      │  │ - androidLint   │
│ - ktlint/detekt │  │ - runScenarios  │  │ - checkLocks    │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         └────────────────────┼────────────────────┘
                              ▼
                 ┌───────────────────────────┐
                 │ Job 4: Integration Gate   │
                 │ - Steward Merge Status    │
                 └───────────────────────────┘
```

#### Pipeline Specifications:
1. **Schema & Contract Validation**:
   - Runs `tooling/scripts/validate-schemas.sh` using `ajv-cli` or Kotlin schema validation against all fixtures in `contracts/fixtures/`.
2. **Core Unit & Scenario Evaluation**:
   - Runs pure Kotlin tests: `./gradlew :core:test`.
   - Runs the evaluation scenario test suite: `./gradlew :evals:runScenarios`.
   - Generates pass/fail/inconclusive breakdown against golden scenario vectors.
3. **Android Build Verification**:
   - Runs `./gradlew :android:assembleDebug :android:lintDebug`.
   - Validates that Android manifest permissions match declared capabilities.
4. **Reproducibility & Dependency Locking**:
   - Pinned Toolchain: JDK 17 (Eclipse Temurin 17.0.x), Gradle Wrapper 8.6+, Android API 34 / Min API 29.
   - Gradle dependency verification enabled (`--write-verification-metadata sha256` in `gradle/verification-metadata.xml`).
   - Strict lockfile enforcement: `./gradlew --dependency-verification=strict`.
5. **Secret Handling & Leak Detection**:
   - Zero hardcoded API keys or test credentials in repository.
   - CI environment injects dummy credentials (`ORBIT_MODEL_API_KEY=mock-ci-key`) for hermetic offline test runs.
   - Automated git pre-commit hook + CI step scanning for high-entropy secrets (e.g. gitleaks rule regexes).
6. **Artifact Naming Standard**:
   - Android APK: `orbit-app-<version>-<git-sha>-<buildType>.apk` (e.g. `orbit-app-v0.1.0-alpha.1-7b3f91a-debug.apk`)
   - Evaluation Report: `eval-summary-<git-sha>-<timestamp>.json`

---

### 4. REPO-004: Handoff Artifact Convention & Archive Management

1. **Adoption of Standard Protocol**:
   - Standard text deliverables: `HANDOFF_<WORK-ITEM>_<FROM>_TO_<TO>.md`
   - Multi-asset deliverables: `HANDOFF_<WORK-ITEM>_<FROM>_TO_<TO>.zip` containing `HANDOFF.md` at zip root and supporting artifacts in `artifacts/`.
   - Single-file rule: Zero loose sidecar files accompanying formal handoffs.
2. **Repository Archive Location**:
   - Accepted handoffs must be committed to the repository under `handoffs/M<milestone>/`:
     ```text
     handoffs/
     ├── M0/
     │   ├── HANDOFF_M0-WAVE-01_ARCH-TL_TO_PM.md
     │   ├── HANDOFF_M0-WAVE-01_PRODUCT-RESEARCH-TL_TO_PM.md
     │   ├── HANDOFF_M0-WAVE-01_QA-SAFETY-TL_TO_PM.md
     │   └── HANDOFF_M0-WAVE-01_REPOSITORY-STEWARD_TO_PM.md
     └── README.md
     ```
   - This keeps the documentation tree (`docs/`) dedicated to evergreen architecture and design documents while maintaining an immutable milestone audit trail in `handoffs/`.

---

## Deliverables

1. **Repository Layout Specification**: Full directory taxonomy and module separation rules detailed in Section 1.
2. **Branch & Integration Policy**: Role permissions, review requirements, Conventional Commit rules, and branch protection specifications detailed in Section 2.
3. **CI & Reproducibility Matrix**: CI jobs, Gradle lockfile verification setup, secret policy, and deterministic build parameters detailed in Section 3.
4. **Handoff Protocol Governance**: Handoff packaging and archiving standard detailed in Section 4.
5. **Bootstrap Skeleton Script**: Initial repository structure generator in `tooling/scripts/init-repo.sh` (provided below).

```bash
#!/usr/bin/env bash
# tooling/scripts/init-repo.sh - Bootstrap Orbit Directory Skeleton
set -euo pipefail

mkdir -p .github/workflows
mkdir -p android/app/src/main/java/org/orbit/android
mkdir -p android/app/src/main/res
mkdir -p core/src/main/kotlin/org/orbit/core/{engine,memory,policy,model}
mkdir -p core/src/test/kotlin/org/orbit/core
mkdir -p contracts/schemas
mkdir -p contracts/fixtures
mkdir -p evals/scenarios
mkdir -p evals/harness
mkdir -p evals/reports
mkdir -p handoffs/M0
mkdir -p docs/{adr,specs,security}
mkdir -p spikes
mkdir -p tooling/{scripts,git-hooks}

echo "# Orbit" > README.md
echo "# Architectural Decision Records" > docs/adr/README.md
echo "# Shared Contracts" > contracts/README.md
echo "# Handoff Archive" > handoffs/README.md
echo "# Spikes & Experiments" > spikes/README.md

echo "Orbit repository skeleton successfully initialized."
```

---

## Evidence

1. **Cross-Assignment Alignment**:
   - Validated against `01_ARCHITECTURE_TL_ASSIGNMENT.md` (`ARCH-001` contract versioning, `ARCH-002` Android capability spike isolation, `ARCH-003` router boundary).
   - Validated against `02_PRODUCT_RESEARCH_TL_ASSIGNMENT.md` (`PROD-001` policy separation, `PROD-002` golden scenario harness in `evals/`).
   - Validated against `03_QA_SAFETY_TL_ASSIGNMENT.md` (`QA-001` privacy boundaries, `QA-002` permission adversarial test vectors in `evals/`, `QA-004` M0 release gate enforcement).
2. **Constraint Verification**:
   - `backend/` omitted for M0: prevents wasted infrastructure setup and conforms to local-first mobile design.
   - `core/` pure Kotlin isolation: ensures scenario evaluations run in < 10 seconds locally and in CI without Android SDK coupling.
   - Dual-branch protection (`main`, `integration`): ensures zero unvetted worker code reaches release candidates.

---

## Assumptions

1. **Gradle Multi-Project Build**: Assumes Gradle (Kotlin DSL) as the standard build system for both JVM `core/` and Android modules.
2. **GitHub Actions Compatibility**: Assumes standard Git hosting (GitHub or equivalent) supporting protected branch rules, required status checks, and environment secret injection.
3. **M0 Team Scale**: Assumes a lean team (PM pair, Architecture TL, Product/Research TL, QA/Safety TL, Repository Steward, and 1-3 targeted workers), making lightweight PR-based reviews fast and non-bureaucratic.

---

## Risks / Blockers

1. **Risk: Android Emulator in CI**: Running full Android UI or instrumentation tests in CI is slow and flaky.
   - *Mitigation*: Strictly enforce that all ambient reasoning, state machine transitions, event deduplication, and policy decisions live in `core/` under pure JVM unit tests and scenario harnesses. Android instrumentation tests are reserved for hardware/API spikes (`ARCH-002`).
2. **Risk: Secret Leakage via Model API Keys**:
   - *Mitigation*: Enforce pre-commit hooks and reject any PR that includes raw API keys or live tokens. CI scenario evaluations must use deterministic mock model responses.
3. **Risk: Contract Drift Between Architecture and Implementation**:
   - *Mitigation*: `contracts/` is single-source-of-truth. CI fails immediately if test fixtures or serialized models diverge from `contracts/schemas/`.

---

## Contract Changes Requested

`None` (The proposed repository layout creates the housing for contracts but does not alter any existing architectural schema).

---

## PM / Product Decisions Needed

`None` (Standard repository stewardship governance. Ready for PM approval to apply branch rules and repository skeleton).

---

## Recommended Next Action

**Action**: Approve M0 repository initialization and authorize the Repository Steward to bootstrap the directory skeleton, git configuration, branch protections on `integration` and `main`, and the baseline CI workflow.  
**Proposed Owner**: `Orbit PM Pair` (Product Owner / Human PM & AI PM).
