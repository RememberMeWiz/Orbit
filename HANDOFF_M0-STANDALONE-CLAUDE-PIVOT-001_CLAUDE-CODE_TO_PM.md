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
| HEAD | `c956aff63abf11492f766af12d510d65793c932e` |
| Commits | 2 on standalone (+1 transport checkpoint) |
| Net change | 11 files, +1306 |
| Final test totals | **91 pass** — 51 workflow + 26 standalone + 14 native gates, 0 skips |
| Verified from | clean `git archive` export, not the working tree |
| Claude usage | ~2h15m consumed this window; ~45m of the ~3h allocation left |

### The north-star answer

> **If every external AI service disappears today, how much of Orbit still works by itself?**

Orchestration: **all of it.** Reasoning quality: **none of it yet.**

A real multi-role work item now runs WORKER → TL → QA locally, with the network severed and 15 vendor environment variables stripped, and correctly stops at the QA→PM approval gate. Zero human courier steps, zero external AI calls. What is *not* yet real is the reasoning: the deterministic provider validates the machinery, it does not think. That gap is one PM decision wide, and §9 below states it exactly.

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

Every import in the runtime is Python stdlib or an internal Orbit module. **The accepted Workflow MVP was already a standalone chassis.** Nothing had to be removed to make Orbit offline-capable — the missing pieces were exactly the ones this directive names: brain, agents, scheduler.

### Host inference hardware

```text
CPU   Intel Core i5-9300H @ 2.40GHz, 4 cores / 8 threads
RAM   15.9 GB
GPU   NVIDIA GeForce GTX 1650, 4096 MiB VRAM (driver 560.70, nvidia-smi present)
      Intel UHD Graphics 630 (integrated)
```

### Installed local inference inventory

```text
torch          2.13.0+cpu     <- CPU-only build; torch.cuda.is_available() == False
onnxruntime    1.27.0         <- providers: AzureExecutionProvider, CPUExecutionProvider
numpy          2.5.1
scipy          1.18.0
transformers   NOT INSTALLED
ollama / llama.cpp / LM Studio / GPT4All / llamafile / vLLM   NONE
model weights (*.gguf, *.safetensors, *.bin)                 NONE FOUND
```

Inference *runtimes* exist; **model weights do not**, and the torch build has no CUDA. §7 forbids multi-GB downloads without approval, so no real local reasoning model could be exercised. Per §8 I built the architecture and report the exact activation step rather than faking a reasoning trial.

### Why the transport burst was checkpointed rather than finished

Independent of this directive, the Claude transport work had already hit a hard blocker. The installed CLI (`2.1.234`) reports:

```json
{ "loggedIn": false, "authMethod": "none", "apiProvider": "firstParty" }
```

— identically with inherited and with cleaned environment. This session's auth is injected by the desktop host process (`CLAUDE_CODE_HOST_SESSION_ID`, `SDK_HAS_HOST_AUTH_REFRESH`) and is not available to a spawned CLI. `ANTHROPIC_BASE_URL` is the real API endpoint, not a local gateway, so there was no local auth path to inherit. Making the CLI authenticate is a credentials decision reserved to PM under §14 of the transport handoff, and I did not read, reuse, or engineer around `~/.claude/.credentials.json`.

That is precisely the external-dependency fragility this pivot removes: the accelerator was unavailable, and under the old plan Orbit would have had nothing to fall back on.

---

## 3. Standalone Architecture Implemented

### `standalone/brain` — provider-neutral reasoning

`LocalBrainRequest` / `LocalBrainResult` plus a `validate_result` gate that returns **typed failures rather than raising**, so a misbehaving model degrades into a record instead of unwinding workflow state. It rejects:

- capability escalation (`used_capabilities` ⊄ `allowed_capabilities`);
- authority directives in results (`allowed_executor_operations`, `destinations`, `acceptance_criteria`, `permissions`, …);
- task-identity mismatch;
- result-schema violations.

`BrainRouter` tries providers in policy order. An unavailable, throwing, or quota-exhausted provider is **skipped, not fatal**; if none can answer it returns typed `BLOCKED`. Network providers are ineligible unless network is explicitly allowed, so the default configuration cannot reach for a cloud service.

`DeterministicBrain` — fully local, offline, rule-based. Validates orchestration mechanics exactly. **It is not a reasoning model and is not presented as one.**

`LocalModelBrain` — the real-model seam. Wired and fail-closed: with no weights, `available()` is `False`, the router skips it like any other unavailable accelerator, and Orbit keeps running. **Activation is configuration, not code.**

### `standalone/agents` — local agent runtime

`AgentTask` identity is derived from `(work_item, role, objective)`, so re-registering after a restart returns the existing record with its status intact. Durable ledger, restart-safe, cross-work-item tasks rejected, STOP prevents new work.

**`COMPLETE` is structurally unreachable from brain output.** The only path is `mark_complete`, called by governed code after the engine actually accepted a handoff, and it refuses any task not already `READY_FOR_REVIEW`. Model output, process exit, and silence can none of them produce completion.

### `standalone/scheduler` — local orchestrator

Reads `WorkflowState`, creates a task for the owning role, runs it locally, and writes a **normal handoff artifact into the workflow inbox** so the accepted engine validates and routes it exactly as it would a human-delivered one. Artifacts are written temp-then-replace so the reconciler never observes a torn file.

It deliberately does not shortcut the engine: transitions, replay protection, digests, registry-bound routing and approval gates all remain the engine's decisions. Blockers and product decisions surface rather than being guessed at.

### Executor seam (design only, per §13)

No new executor operation was added. `allowed_executor_operations` remains exactly `["PLACE_PACKET"]`. The capability vocabulary is already threaded through `AgentTask.allowed_capabilities` and enforced by the brain gate, so typed operations (`READ_FILE`, `WRITE_FILE_IN_APPROVED_ROOT`, `RUN_APPROVED_TEST`, …) can be added later one at a time, each separately permissioned, without touching the brain or scheduler.

### Files and commits

```text
c956aff  fix(scheduler): do not report a gated transition as advanced
d160e8b  feat(standalone): local brain, agent runtime and scheduler kernel

standalone/brain/contracts.py       178   request/result contracts + authority gate
standalone/brain/providers.py       176   router, deterministic + local-model providers
standalone/agents/runtime.py        245   AgentTask, durable ledger, runtime
standalone/scheduler/scheduler.py   273   scheduler + emission ledger
standalone/tests/test_standalone.py 381   26 tests
windows/run_tests.ps1                +4   standalone suite added to the validated floor
```

---

## 4. Offline Trial (Phase E)

Run from a **clean `git archive` export of the committed branch**, against a freshly bootstrapped real work item, with the real bootstrapper, engine, validator, executor and reconciler all participating.

### Conditions

```text
network             SEVERED — socket.socket, socket.create_connection and
                    socket.getaddrinfo all raise on any call
vendor env stripped 15 vars (ANTHROPIC_BASE_URL, CLAUDECODE, CLAUDE_CODE_*, …)
providers           [local-model, deterministic-local]   (local-model unavailable)
external AI invoked NONE — no Claude, no ChatGPT, no Antigravity
```

### Result

```text
work item      M0-WF-LIVE-003   (Orbit / orbit-m0-live-trial)
executor       ["PLACE_PACKET"]
initial        owner=WORKER work_state=ASSIGNED rev=1

tick 1  ADVANCED           WORKER -> TL   status=COMPLETE  handoff=local-72b2307d…  provider=deterministic-local
tick 2  ADVANCED           TL -> QA       status=COMPLETE  handoff=local-359f9d15…  provider=deterministic-local
tick 3  AWAITING_APPROVAL  QA

final          owner=QA work_state=READY_FOR_REVIEW delivery=APPROVAL_PENDING rev=4
accepted       3 handoffs, all prefixed "local-" (Orbit-produced, not human-carried)
receipts       3 written, 3 accepted
packets        TL: 1, QA: 1
agent tasks    WORKER COMPLETE, TL COMPLETE, QA COMPLETE — attempts=1 each

HUMAN COURIER STEPS : 0
EXTERNAL AI CALLS   : 0
```

The QA→PM approval gate held correctly: the artifact was accepted, the owner stayed QA, and `delivery_state` went `APPROVAL_PENDING`. That is the system surfacing a real product decision instead of advancing through it.

### What this does and does not prove

**Proves:** the orchestration mechanics are genuinely standalone. Local roles produce real handoffs, the accepted engine validates and routes them, state persists, gates hold, and no external service participates.

**Does not prove:** useful autonomous reasoning. The deterministic provider follows rules; it does not think. **I am not claiming standalone autonomous reasoning, and no real local reasoning model completed a task.**

---

## 5. Architecture-Style Disposition

**COMPATIBLE.**

- No shared contract changed. No manifest schema, handoff envelope, receipt schema, or state schema touched.
- No acceptance criterion changed. The floor was **raised** (51 → 91 tests), never lowered.
- `allowed_executor_operations` remains exactly `["PLACE_PACKET"]`, enforced unchanged by `place_packet.py`, which this burst did not modify.
- The standalone kernel is strictly additive: it sits beside `workflow/` and `windows/` and imports only `workflow.core.storage` helpers plus the accepted engine/reconciler at the scheduler boundary. Removing `standalone/` entirely leaves the accepted system exactly as it was.
- Optional-provider abstraction holds in both directions: an external brain can be registered behind `BrainProvider`, and its absence changes no core semantics.

---

## 6. QA-Style Disposition

**QA_GO.** 26 new adversarial tests, all green, plus the full existing floor.

| Case | Covers | Result |
| :--- | :--- | :--- |
| `OFFLINE-001` | boots with vendor credentials removed | PASS |
| `OFFLINE-002` | **full orchestration with sockets severed** | PASS |
| `OFFLINE-003` | no external adapter registered at all → core operational | PASS |
| `OFFLINE-004` | network provider ineligible by default | PASS |
| `OFFLINE-005` | quota-exhausted provider falls back locally | PASS |
| `OFFLINE-006` | no provider available → typed BLOCKED, not a crash | PASS |
| `BRAIN-001` | result schema enforced | PASS |
| `BRAIN-002` | **brain package structurally cannot reach durable state** | PASS |
| `BRAIN-003` | cannot self-grant capabilities | PASS |
| `BRAIN-004` | task-identity mismatch rejected | PASS |
| `AUTH-001` | result carrying authority directives rejected | PASS |
| `AUTH-002` | external adapter gets no privileged path | PASS |
| `AGENT-001` | WORKER task returns structured result | PASS |
| `AGENT-002` | BLOCKED preserved | PASS |
| `AGENT-003` | NEEDS_DECISION preserved | PASS |
| `AGENT-004` | restart does not duplicate or re-run | PASS |
| `AGENT-005` | STOP prevents new agent work | PASS |
| `AGENT-006` | wrong work-item task rejected | PASS |
| `AGENT-007` | COMPLETE unreachable from brain output | PASS |
| `SCHED-001` | WORKER→TL advances exactly once, one packet | PASS |
| `SCHED-002` | full local chain reaches the approval gate | PASS |
| `SCHED-003` | blocked role routes to registered escalation, then surfaces | PASS |
| `SCHED-004` | product decision stops instead of guessing | PASS |
| `SCHED-005` | STOP halts scheduling | PASS |
| `SCHED-006` | restart mid-chain does not double-advance | PASS |
| `SCHED-007` | every accepted handoff was Orbit-produced | PASS |

`OFFLINE-002` and `BRAIN-002` are the two that carry real weight: the first severs socket creation and *then* runs the whole chain, so it proves absence of network use rather than asserting it; the second is a structural check that the brain package never imports a durable-state type, so a model result has no code path to mutate state.

Existing regression: replay/digest/idempotency, path/junction safety, restart recovery, reconciler smoke and the secret/canary scan all remain green. Clean-export validation reproduces.

**One defect found and fixed during self-review:** the scheduler reported `ADVANCED` for the gated QA→PM step when the engine had actually held it at `APPROVAL_PENDING`. Persisted state was correct throughout; only the reported action overstated what happened — which matters, because that string is what a PM reads to decide whether attention is needed. Fixed in `c956aff`.

---

## 7. PM-Style Disposition

**Accepted** (inside the frozen scope of this directive):

- standalone architecture: LocalBrain, local agent runtime, local scheduler;
- deterministic local end-to-end orchestration of a real work item;
- no hard external dependency anywhere in Orbit core;
- optional-provider abstraction with guaranteed local fallback;
- regression floor raised to 91 with clean-export reproducibility.

**Not claimed:**

- standalone autonomous *reasoning* — no real local model ran;
- that the deterministic provider is a substitute for one;
- Claude/ChatGPT/Antigravity transport — checkpointed, unmerged, blocked on a credential decision;
- any change to executor authority.

This meets the directive's stated **minimum acceptable progress** in full, and meets the **best case** for everything except real local reasoning.

---

## 8. Remaining Gaps, Ranked by Leverage

**1. No local reasoning model — the only thing between here and real autonomy.**
Everything else is built and tested. The kernel runs, the seam is wired, and `LocalModelBrain` is one configuration away. Until weights exist, Orbit orchestrates deterministically but does not think. This is a PM decision, not an engineering task: see §9.

**2. Scheduler covers the internal role chain, not objective intake.**
A human still states the objective and bootstraps the work item. The chain from there is autonomous, but Orbit cannot yet take "here is what I want" and create the work item itself. Roughly 1–2 hours once a brain exists, because the objective→work-item step is the first thing that genuinely needs reasoning.

**3. Typed local executor is designed but empty.**
`allowed_capabilities` is threaded through and enforced, but no capability is implemented, so local agents can reason and hand off — they cannot yet read a file, run a test, or write into an approved root. Each operation needs its own Architecture/QA gate. `READ_FILE` and `RUN_APPROVED_TEST` are the two highest-value first candidates.

---

## 9. The Exact Local-Model Activation Step

This is the single decision that converts deterministic orchestration into real local reasoning. The host constrains the options:

```text
GTX 1650, 4 GB VRAM   -> comfortably runs a 3B–7B model at Q4 quantisation
15.9 GB RAM           -> 7B–8B on CPU, slowly
torch 2.13.0+cpu      -> CPU-only build; GPU use needs a CUDA wheel or a GGUF runtime
```

Minimum viable activation, roughly 4–6 GB of downloads:

1. install a local GGUF runtime (`llama-cpp-python` with CUDA, or the Ollama service);
2. pull one small instruct model (a 7B-class Q4_K_M GGUF, ~4 GB, fits 4 GB VRAM with partial offload);
3. point `LocalModelBrain` at it and register it ahead of `DeterministicBrain` — **no code change**, the router already prefers it and falls back automatically if it fails.

**I did not do this**: §7 requires explicit Product Owner approval before downloading model weights or installing a large runtime, and the choice of local model vendor is a product decision, not mine to make. The architecture is deliberately not bound to any one runtime.

---

## 10. Safety Statement

Explicitly confirmed:

- **Orbit runtime executor authority unchanged** — `allowed_executor_operations` is exactly `["PLACE_PACKET"]`; `place_packet.py` was not modified in this burst.
- **No new dangerous authority.** No shell, Git, browser, GUI, network, or credential capability added to Orbit. The standalone kernel makes zero subprocess calls and zero network calls.
- **Claude's machine privilege was not used to justify Orbit authority.** I hold broad development permissions in this session; Orbit gained none of them. That separation is enforced structurally — the brain package cannot reach durable state, and a model result cannot grant a capability.
- **No secrets touched.** `~/.claude/.credentials.json` was identified as secret-bearing and never read, copied, or referenced. No credential entered any Orbit artifact, receipt, or audit record. Post-run secret/canary scan PASS, 0 hits across 20 evidence files.
- **No shared branch moved.** `origin/main` remains `6928e5b`; `origin/integration` remains `d836bf7`. Verified after pushing.
- **No force-push, no history rewrite, no branch deletion, no merge to a shared branch.**
- **No product, contract, or acceptance-criteria change.** The floor was raised, never lowered; nothing was waived to make a gate pass.
- **Transport work quarantined.** The Claude-specific adapter is checkpointed on its own branch and was deliberately **not** dragged into the standalone core, per §1.

---

## 11. Next Recommended Action

**Action.** Approve (or decline) the local-model activation in §9 — a ~4 GB GGUF model plus a local runtime — and name the model. That single decision is what converts the now-proven deterministic orchestration into real local reasoning; every other standalone blocker is downstream of it.

**Proposed Owner.** `Orbit PM Pair` (Product Owner decides the download and the model; `Architecture TL` confirms the runtime choice stays vendor-neutral behind `LocalModelBrain`).

---

## Final Status

```text
COMPLETE_WITH_PROGRESS
```

### Clean continuation point

`claude/m0-standalone-runtime-001` @ `c956aff`, pushed. 91 tests pass from a bare `git archive` export with 0 release-blocking skips. The offline trial is reproducible on demand. `claude/m0-wf-transport-burst-001` @ `8683d18` holds the agent-neutral transport seam, unmerged and clearly labelled, for whenever external accelerators become worthwhile again.

Stopped because the standalone kernel reached a coherent, fully verified state and the next meaningful step requires a Product Owner decision — not because budget ran out. Roughly 45 minutes of the allocation remains in reserve.
