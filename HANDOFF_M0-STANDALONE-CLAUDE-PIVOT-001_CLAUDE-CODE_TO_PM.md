# Orbit Handoff

## Header
- Work Item: `M0-STANDALONE-CLAUDE-PIVOT-001`
- From: `Claude Code Ultra / Opus 5`
- To: `Orbit PM Pair (Product Owner / Human PM & AI PM)`
- Status: `COMPLETE_WITH_PROGRESS`
- Handoff ID: `m0-standalone-claude-pivot-001-claude-code-to-pm-001`
- Sequence: `1`
- Date: `2026-08-19`
- Contract Version(s): `orbit.workflow-contracts/0.1-draft` — no contract change requested

---

## 1. Executive Status

| Item | Value |
| :--- | :--- |
| Status | `COMPLETE_WITH_PROGRESS` |
| Old burst checkpoint | `claude/m0-wf-transport-burst-001` @ `8683d18` (pushed, **unmerged**, deliberately) |
| New standalone branch | `claude/m0-standalone-runtime-001` (pushed) |
| Baseline | `d836bf70c57ab175002e717410d7e0493d12866a` |
| HEAD | `11ba94c52f6f02c688feb6c1a4e1d60c2728feed` |
| Commits | 4 on standalone (+1 transport checkpoint) |
| Final test totals | **103 pass** — 51 workflow + 38 standalone + 14 native gates, 0 skips |
| Verified from | clean `git archive` export (91-test state); 103-test state verified in-tree |
| Real local reasoning | **NO** — model installed and wired, never successfully executed |
| Claude usage | ~2h50m consumed; the ~3h allocation is essentially spent |

### The north-star answer

> **If every external AI service disappears today, how much of Orbit still works by itself?**

**Orchestration: all of it. Reasoning: still none of it.**

A real multi-role work item runs WORKER → TL → QA locally with the network severed and 15 vendor environment variables stripped, then correctly stops at the QA→PM approval gate. Zero human courier steps, zero external AI calls.

Since the last handoff the Product Owner approved a local-model download, so a real reasoning backend is now **installed and wired** — but it has **not produced a single successful inference**. See §5. I am not claiming local reasoning.

---

## 2. Dependency Audit (Phase A)

### External dependencies in Orbit runtime: none

Scanned `artifacts/workflow`, `artifacts/windows` and `live_runner.py`, excluding tests:

```text
network imports (socket/urllib/requests/http/httpx)   0
vendor SDKs (anthropic/openai/google.generativeai)    0
credential references (api_key/API_KEY/Bearer/token)  0   [1]
subprocess / os.system / popen                        0
third-party packages                                  0
```

[1] The only `token` matches are SHA-256-derived packet filename tokens in `place_packet.py`, not authentication.

**The accepted Workflow MVP was already a standalone chassis.** Nothing had to be removed to make Orbit offline-capable; the missing pieces were exactly brain, agents and scheduler.

### Host inference hardware

```text
CPU   Intel Core i5-9300H @ 2.40GHz, 4 cores / 8 threads
RAM   15.9 GB
GPU   NVIDIA GeForce GTX 1650, 4096 MiB VRAM (driver 560.70)
      Intel UHD Graphics 630 (integrated)
Disk  C: 13 GB free of 118 GB  |  E: 55 GB free of 447 GB
```

### Local inference inventory — now installed

Before this burst: `torch 2.13.0+cpu`, `onnxruntime 1.27.0` (both CPU-only), no `transformers`, **no model weights**.

Product Owner approved a download constrained to drive `E:`. Installed entirely off `C:` — **`C:` free space is unchanged at 12.6 GB**:

```text
E:\OrbitLocalAI\runtime\llama-vulkan    llama.cpp b10488, Vulkan build   33 MB
E:\OrbitLocalAI\runtime\llama-cpu       llama.cpp b10488, CPU build      18 MB
E:\OrbitLocalAI\models\Phi-3.5-mini-instruct-Q4_K_M.gguf                2.23 GB
  sha256 e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5
```

**Model choice.** Phi-3.5-mini-instruct Q4_K_M for its **MIT licence** — cleanest of the candidates for a product — and because 2.23 GB nominally fits the GTX 1650's 4 GB VRAM. Alternatives checked and available: Qwen2.5-3B (1.80 GB, Qwen Research licence) and Qwen2.5-7B (4.36 GB, Apache-2.0). Qwen2.5-7B is the quality upgrade path, at the cost of partial CPU offload.

### Why the transport burst was checkpointed

Independent of the pivot, Claude transport had already hit a wall. The installed CLI (`2.1.234`) reports `{"loggedIn": false, "authMethod": "none"}` with both inherited and cleaned environment. This session's auth is injected by the desktop host process and is unavailable to a spawned CLI; `ANTHROPIC_BASE_URL` is the real API endpoint, not a local gateway. Making it authenticate is a credentials decision reserved to PM, and I did not read, reuse, or work around `~/.claude/.credentials.json`.

That is precisely the fragility this pivot removes: the accelerator was unavailable, and under the old plan Orbit had no fallback.

---

## 3. Standalone Architecture Implemented

### `standalone/brain` — provider-neutral reasoning

`LocalBrainRequest` / `LocalBrainResult` plus a `validate_result` gate returning **typed failures rather than raising**, so a misbehaving model degrades into a record instead of unwinding workflow state. It rejects capability escalation, authority directives (`allowed_executor_operations`, `destinations`, `acceptance_criteria`, …), task-identity mismatch, and schema violations.

`BrainRouter` tries providers in policy order. Unavailable, throwing, or quota-exhausted providers are **skipped, not fatal**; if none can answer it returns typed `BLOCKED`. Network providers are ineligible unless explicitly allowed.

Providers:

- `DeterministicBrain` — fully local, rule-based. Validates orchestration mechanics exactly. **Not a reasoning model and not presented as one.**
- `LlamaCppBrain` — **new this burst.** Real local reasoning via a llama.cpp subprocess reading GGUF weights from disk. No internet, no quota, no token; explicitly permitted by §3 of the directive.
- `LocalModelBrain` — generic fail-closed seam retained for other runtimes.

`LlamaCppBrain` keeps Orbit's authority discipline: executable and model paths are fixed from trusted configuration at construction and are **never** read from a request, objective, or handoff prose; the process is spawned with an argument list and no shell, so hostile text is one opaque argument that cannot become a flag or a second command.

### `standalone/agents` — local agent runtime

`AgentTask` identity derived from `(work_item, role, objective)`; durable ledger; restart-safe; cross-work-item tasks rejected; STOP prevents new work.

**`COMPLETE` is structurally unreachable from brain output.** The only path is `mark_complete`, called by governed code after the engine accepted a handoff, and it refuses any task not already `READY_FOR_REVIEW`.

### `standalone/scheduler` — local orchestrator

Reads `WorkflowState`, creates a task for the owning role, runs it locally, writes a **normal handoff artifact into the workflow inbox** so the accepted engine validates and routes it. Written temp-then-replace so the reconciler never sees a torn file. Transitions, replay protection, digests, registry routing and approval gates all remain the engine's decisions.

### Files and commits

```text
11ba94c  feat(brain): add llama.cpp local reasoning provider
d38700d  docs(handoff): record standalone-first pivot handoff to PM
c956aff  fix(scheduler): do not report a gated transition as advanced
d160e8b  feat(standalone): local brain, agent runtime and scheduler kernel

standalone/brain/contracts.py            178   contracts + authority gate
standalone/brain/providers.py            176   router, deterministic, seam
standalone/brain/llama_cpp.py            203   real local model provider
standalone/agents/runtime.py             245   AgentTask, ledger, runtime
standalone/scheduler/scheduler.py        281   scheduler + emission ledger
standalone/tests/test_standalone.py      381   26 tests
standalone/tests/test_llama_cpp_brain.py 145   12 tests
windows/run_tests.ps1                     +4   standalone suite added to floor
```

---

## 4. Offline Trial (Phase E)

Run from a **clean `git archive` export**, against a freshly bootstrapped real work item, with the real bootstrapper, engine, validator, executor and reconciler all participating.

```text
network             SEVERED — socket.socket, create_connection, getaddrinfo all raise
vendor env stripped 15 vars (ANTHROPIC_BASE_URL, CLAUDECODE, CLAUDE_CODE_*, …)
external AI invoked NONE

initial   owner=WORKER work_state=ASSIGNED rev=1
tick 1    ADVANCED           WORKER -> TL   handoff=local-72b2307d…
tick 2    ADVANCED           TL -> QA       handoff=local-359f9d15…
tick 3    AWAITING_APPROVAL  QA
final     owner=QA work_state=READY_FOR_REVIEW delivery=APPROVAL_PENDING rev=4

accepted  3 handoffs, all "local-" prefixed (Orbit-produced, not human-carried)
receipts  3 written, 3 accepted
packets   TL: 1, QA: 1
tasks     WORKER/TL/QA all COMPLETE, attempts=1 each

HUMAN COURIER STEPS : 0
EXTERNAL AI CALLS   : 0
```

The QA→PM approval gate held correctly — artifact accepted, owner stayed QA, `delivery_state` went `APPROVAL_PENDING`. The system surfacing a real product decision rather than advancing through it.

**Proves:** orchestration is genuinely standalone. **Does not prove:** useful autonomous reasoning.

---

## 5. Local Model: Installed, Wired, Never Executed

This is the part that did not land, stated plainly.

### What was attempted

| # | Configuration | Outcome |
| :-- | :--- | :--- |
| 1 | Vulkan, `-ngl 99` | `ErrorOutOfDeviceMemory` allocating a 64 MB compute buffer |
| 2 | Vulkan, `-ngl 99 -c 4096` | Same failure — so it is the compute buffer, not the 128K-context KV cache |
| 3 | CPU build via `LlamaCppBrain`, 3/8 threads, 64 tokens, 240 s cap | **Timed out at 242 s**, no output |
| 4–5 | Retunes (smaller Vulkan batch; CPU at 5/8 threads, 32 tokens) | **Declined by Product Owner** |

The Vulkan failure is notable: `nvidia-smi` reported **3838 MiB free of 4096**, so a 64 MB allocation failing points at a Vulkan heap/driver limitation on the GTX 1650 rather than genuine exhaustion.

After the third decline I stopped attempting execution rather than continuing to retry variants on the Product Owner's machine.

### What attempt 3 did prove — and it is worth having

The 240 s timeout was a **real** failure, not a stub. The router handled it exactly as designed:

```text
provider attempts : [{'provider': 'llama-cpp-local',   'outcome': 'FAILED_RETRYABLE'},
                     {'provider': 'deterministic-local','outcome': 'OK'}]
final status      : OK   (work completed via fallback)
```

A local model that hangs did not stall Orbit, did not corrupt state, and did not raise into workflow code — the work item completed on the fallback provider. That is live evidence for the directive's §14 invariant ("no quota exhaustion should crash Orbit"), obtained against a genuine fault rather than a mock.

### Honest status

```text
LOCAL MODEL: installed, wired, unit-tested, NEVER SUCCESSFULLY EXECUTED
REAL LOCAL REASONING RESULT: none
```

---

## 6. Architecture-Style Disposition

**COMPATIBLE.**

- No shared contract changed — no manifest, envelope, receipt or state schema touched.
- No acceptance criterion changed. The floor was **raised** (51 → 103 tests), never lowered.
- `allowed_executor_operations` remains exactly `["PLACE_PACKET"]`; `place_packet.py` untouched.
- The standalone kernel is strictly additive. Deleting `standalone/` leaves the accepted system exactly as it was.
- Optional-provider abstraction holds both ways: a cloud brain can register behind `BrainProvider`, and its absence changes no core semantics. The local model is itself just another optional provider — its unavailability is already proven harmless.

---

## 7. QA-Style Disposition

**QA_GO** for the standalone kernel. **NOT CLAIMED** for local reasoning.

38 standalone tests plus the existing floor, 103 total, 0 skips.

Highest-value cases:

| Case | Covers |
| :--- | :--- |
| `OFFLINE-002` | **severs socket creation, then runs the full chain** — proves absence of network use rather than asserting it |
| `BRAIN-002` | structural: the brain package cannot import any durable-state type |
| `OFFLINE-005/006` | quota-exhausted provider falls back; no provider yields typed BLOCKED, not a crash |
| `AGENT-004/005/007` | restart does not duplicate; STOP halts; COMPLETE unreachable from brain output |
| `SCHED-003/004` | blocker and product decision route locally then surface, never guessed |
| `SCHED-007` | every accepted handoff in the chain was Orbit-produced |
| `LLAMA-008` | objective containing `& calc.exe --dangerously-skip-permissions -m C:/evil.gguf` lands inside exactly one argv element with the configured model path intact |
| `LLAMA-009` | a model claiming `allowed_executor_operations` is rejected by the contract gate |
| `LLAMA-004/005/006` | unparseable output, timeout and missing binary all degrade to retryable, never fatal |

**Three defects found and fixed during self-review:**

1. The scheduler reported `ADVANCED` for the gated QA→PM step the engine had actually held at `APPROVAL_PENDING`. State was always correct; the reported action overstated it — which matters, because that string is what tells a PM whether to look. (`c956aff`)
2. `LlamaCppBrain.available()` consulted only the filesystem, so an injected runner could never be reached. An injected runner now means the caller owns execution, matching `LocalModelBrain`.
3. A test compared path strings where `Path` normalises separators on Windows.

Existing regression — replay/digest/idempotency, path/junction safety, restart recovery, reconciler smoke, secret/canary scan — all green.

---

## 8. PM-Style Disposition

**Accepted:**

- standalone architecture: LocalBrain, local agent runtime, local scheduler;
- deterministic local end-to-end orchestration of a real work item, offline;
- no hard external dependency anywhere in Orbit core;
- optional-provider abstraction with guaranteed local fallback, **now proven against a real fault**;
- local model installed on `E:` with zero `C:` footprint, provider wired and unit-tested;
- regression floor raised to 103.

**Not claimed:**

- standalone autonomous *reasoning* — the model has never produced an inference;
- that the deterministic provider substitutes for one;
- Claude/ChatGPT/Antigravity transport — checkpointed, unmerged, blocked on credentials;
- any change to executor authority.

This meets the directive's **minimum acceptable progress** in full. It does not meet the best case, solely because no local inference has run.

---

## 9. Remaining Gaps, Ranked by Leverage

**1. The local model has never produced an inference.**
Everything around it is built, wired and tested; the gap is now a single successful invocation. Two things must be settled: whether the Vulkan heap limitation on the GTX 1650 can be worked around (smaller batch via `-b`/`-ub`, or partial `-ngl`), and what CPU-only throughput actually is — the one CPU attempt timed out at 240 s with 3 threads, which is uninformative because it never reported whether time went to model load or generation. A 5-minute diagnostic at a moment convenient to the Product Owner would settle both. If Phi-3.5-mini proves too slow on this hardware, Qwen2.5-3B (1.80 GB) is the lighter fallback.

**2. Scheduler covers the internal role chain, not objective intake.**
A human still states the objective and bootstraps the work item. The chain from there is autonomous. Roughly 1–2 hours once a brain actually answers, because objective→work-item is the first step that genuinely needs reasoning.

**3. Typed local executor is designed but empty.**
`allowed_capabilities` is threaded through and enforced, but no capability is implemented — local agents can reason and hand off, not read a file or run a test. Each needs its own Architecture/QA gate; `READ_FILE` and `RUN_APPROVED_TEST` are the best first candidates.

---

## 10. Safety Statement

Explicitly confirmed:

- **Orbit runtime executor authority unchanged** — `allowed_executor_operations` is exactly `["PLACE_PACKET"]`; `place_packet.py` not modified.
- **No new dangerous authority.** No shell, Git, browser, GUI, network, or credential capability added to Orbit. The one subprocess Orbit can now spawn is a fixed, configuration-pinned local inference binary that cannot be redirected by handoff content.
- **Claude's machine privilege was not used to justify Orbit authority.** That separation is enforced structurally: the brain package cannot reach durable state, and a model result cannot grant a capability.
- **Download confined to `E:` as instructed.** `C:` free space unchanged at 12.6 GB before and after. Nothing was installed into `C:`, no system PATH or registry change, no service registered. Removal is `rm -r E:\OrbitLocalAI`.
- **No secrets touched.** `~/.claude/.credentials.json` identified as secret-bearing and never read, copied or referenced. Secret/canary scan PASS, 0 hits across 20 evidence files.
- **No shared branch moved.** `origin/main` `6928e5b`; `origin/integration` `d836bf7`.
- **No force-push, no history rewrite, no branch deletion, no merge to a shared branch.**
- **No product, contract, or acceptance-criteria change.** Floor raised, never waived.
- **Host treated carefully.** The one execution attempt ran at below-normal priority on 3 of 8 threads with a hard timeout; when the Product Owner declined further attempts I stopped rather than retrying.

---

## 11. Next Recommended Action

**Action.** At a moment convenient to the Product Owner, run one bounded local-model diagnostic to determine whether Phi-3.5-mini is viable on this hardware — a single `llama-cli` invocation reporting load time and tokens/sec, Vulkan with reduced batch and CPU as fallback. That one measurement converts the wired-but-unproven provider into either a working local brain or a decision to switch to Qwen2.5-3B. Every remaining standalone blocker sits behind it.

**Proposed Owner.** `Orbit PM Pair` (Product Owner chooses the moment, since it briefly loads the machine).

---

## Final Status

```text
COMPLETE_WITH_PROGRESS
```

### Clean continuation point

`claude/m0-standalone-runtime-001` @ `11ba94c`, pushed. 103 tests pass, 0 release-blocking skips; the 91-test state was additionally verified from a bare `git archive` export. The offline orchestration trial is reproducible on demand. The local model is installed at `E:\OrbitLocalAI` and wired behind `LlamaCppBrain`, needing only a successful invocation. `claude/m0-wf-transport-burst-001` @ `8683d18` holds the agent-neutral transport seam, unmerged.

Stopping because the ~3h allocation is spent and the one remaining step needs the Product Owner's machine at a convenient moment.
