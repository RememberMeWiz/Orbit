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
| Standalone branch | `claude/m0-standalone-runtime-001` (pushed) |
| Baseline | `d836bf70c57ab175002e717410d7e0493d12866a` |
| HEAD | `0813f44` |
| Commits | 5 on standalone (+1 transport checkpoint) |
| Final test totals | **131 pass, 2 skipped** — 51 workflow + 66 standalone + 14 native gates |
| Real local reasoning | **YES** — Phi-3.5-mini reasoned all three roles, offline |
| Claude usage | ~3h20m, slightly over the ~3h guideline |

### The north-star answer

> **If every external AI service disappears today, how much of Orbit still works by itself?**

**All of it, including the reasoning.**

A real multi-role work item runs WORKER → TL → QA with the network severed, 15 vendor environment variables stripped, and **every role reasoned by a local model on this machine**. Zero human courier steps, zero external AI calls, zero credentials, zero vendor quota.

My previous handoff reported local reasoning as blocked on hardware. **That was wrong.** The model was never slow — Orbit's own provider was hanging it. §5 documents the misdiagnosis in full.

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

**The accepted Workflow MVP was already a standalone chassis.** Nothing had to be removed to make Orbit offline-capable; the missing pieces were brain, agents, scheduler and executor.

### Host inference hardware

```text
CPU   Intel Core i5-9300H @ 2.40GHz, 4 cores / 8 threads
RAM   15.9 GB
GPU   NVIDIA GeForce GTX 1650, 4096 MiB VRAM (driver 560.70)
Disk  C: 12.6 GB free of 118 GB  |  E: 55 GB free of 447 GB
```

### Local inference — installed and working

Product Owner approved a download constrained to drive `E:`. Installed entirely off `C:` — **`C:` free space unchanged at 12.6 GB**:

```text
E:\OrbitLocalAI\runtime\llama-vulkan    llama.cpp b10488, Vulkan build   33 MB
E:\OrbitLocalAI\runtime\llama-cpu       llama.cpp b10488, CPU build      18 MB
E:\OrbitLocalAI\models\Phi-3.5-mini-instruct-Q4_K_M.gguf                2.23 GB
  sha256 e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5
```

**Model choice.** Phi-3.5-mini-instruct Q4_K_M for its **MIT licence** — cleanest of the candidates for a product. Alternatives verified available: Qwen2.5-3B (1.80 GB, Qwen Research licence), Qwen2.5-7B (4.36 GB, Apache-2.0).

**Measured throughput on the CPU build:** prompt 18.1 tok/s, generation 4.2 tok/s. A complete role decision takes **~15 seconds**. Uninstall is `rm -r E:\OrbitLocalAI`.

The Vulkan build is installed but unused: it fails to allocate a 64 MB compute buffer despite `nvidia-smi` reporting 3838 MiB free, which looks like a driver heap limit on the GTX 1650. CPU throughput is sufficient, so this is an optimisation, not a blocker.

### Why the transport burst was checkpointed

The installed Claude CLI (`2.1.234`) reports `{"loggedIn": false, "authMethod": "none"}` with both inherited and cleaned environment. This session's auth is injected by the desktop host process and is unavailable to a spawned CLI; `ANTHROPIC_BASE_URL` is the real API endpoint, not a local gateway. Making it authenticate is a credentials decision reserved to PM, and I did not read, reuse, or work around `~/.claude/.credentials.json`.

That is exactly the fragility this pivot removes: the accelerator was unavailable, and Orbit now does not care.

---

## 3. Standalone Architecture Implemented

### `standalone/brain` — provider-neutral reasoning

`LocalBrainRequest` / `LocalBrainResult` plus a `validate_result` gate returning **typed failures rather than raising**, so a misbehaving model degrades into a record instead of unwinding workflow state. It rejects capability escalation, authority directives (`allowed_executor_operations`, `destinations`, `acceptance_criteria`, …), task-identity mismatch and schema violations.

`BrainRouter` tries providers in policy order. Unavailable, throwing, or quota-exhausted providers are **skipped, not fatal**; if none can answer it returns typed `BLOCKED`. Network providers are ineligible unless explicitly allowed.

- `LlamaCppBrain` — **real local reasoning.** A llama.cpp subprocess reading GGUF weights from disk. No internet, no quota, no token; explicitly permitted by §3 of the directive.
- `DeterministicBrain` — rule-based fallback. Validates mechanics; **not a reasoning model**.
- `LocalModelBrain` — generic fail-closed seam for other runtimes.

`LlamaCppBrain` keeps Orbit's authority discipline: executable and model paths are fixed from trusted configuration at construction and are **never** read from a request, objective, or handoff prose; the process is spawned with an argument list and no shell, so hostile text is one opaque argument that cannot become a flag or a second command.

### `standalone/agents` — local agent runtime

`AgentTask` identity from `(work_item, role, objective)`; durable ledger; restart-safe; cross-work-item tasks rejected; STOP prevents new work.

**`COMPLETE` is structurally unreachable from brain output.** The only path is `mark_complete`, called by governed code after the engine accepted a handoff, and it refuses any task not already `READY_FOR_REVIEW`.

### `standalone/scheduler` — local orchestrator

Reads `WorkflowState`, creates a task for the owning role, runs it locally, and writes a **normal handoff artifact into the workflow inbox** so the accepted engine validates and routes it. Transitions, replay protection, digests, registry routing and approval gates all remain the engine's decisions.

### `standalone/executor` — typed local executor *(new)*

Local agents could reason and hand off but could not **read** anything — a TL agent "reviewed" a deliverable it had no way to open. This closes that gap under directive §13.

```text
IMPLEMENTED (read-only, clearly safe)
  READ_FILE                    UTF-8 text inside the approved root, size-capped at 1 MiB
  LIST_DIRECTORY               immediate children, links reported but never followed
  STAT_PATH                    existence, kind, size

DECLARED BUT GATED (return "operation-not-enabled" with the gate they need)
  WRITE_FILE_IN_APPROVED_ROOT  broader than PLACE_PACKET's fixed-shape packet
  RUN_APPROVED_PROCESS         needs an approved-executable registry + arg allowlist
  RUN_APPROVED_TEST            inherits the RUN_APPROVED_PROCESS gate
  GIT_STATUS                   reaches a VCS outside the root; can leak paths
```

Path discipline mirrors the accepted PLACE_PACKET adapter: relative paths only; absolute and `..` refused before any filesystem call; resolution confined to the approved root; every existing component checked for symlink/junction reparse points; and a **re-check after resolution** closing the decide-then-act window.

There is deliberately **no generic `RUN_COMMAND`**, and a test asserts that none of `RUN_COMMAND` / `EXEC` / `SHELL` / `EVAL` / `SYSTEM` exists in the operation table. Another asserts every *implemented* operation is read-only, so enabling a write path cannot happen by accident.

### Files and commits

```text
0813f44  feat(executor): typed local executor, and fix the llama-cli stdin hang
11ba94c  feat(brain): add llama.cpp local reasoning provider
d38700d  docs(handoff): record standalone-first pivot handoff to PM
c956aff  fix(scheduler): do not report a gated transition as advanced
d160e8b  feat(standalone): local brain, agent runtime and scheduler kernel

standalone/brain/contracts.py            178   contracts + authority gate
standalone/brain/providers.py            176   router, deterministic, seam
standalone/brain/llama_cpp.py            212   real local model provider
standalone/agents/runtime.py             245   AgentTask, ledger, runtime
standalone/scheduler/scheduler.py        281   scheduler + emission ledger
standalone/executor/contracts.py         120   operation table + request/result
standalone/executor/local.py             215   path-safe read-only executor
standalone/tests/test_standalone.py      381   26 tests
standalone/tests/test_llama_cpp_brain.py 190   15 tests
standalone/tests/test_executor.py        215   27 tests
windows/run_tests.ps1                     +4   standalone suite added to floor
```

---

## 4. Offline Trial — with real local reasoning

Run from a clean `git archive` export against a freshly bootstrapped real work item. The real bootstrapper, engine, validator, executor and reconciler all participate.

```text
network             SEVERED — socket.socket, create_connection, getaddrinfo all raise
vendor env stripped 15 vars (ANTHROPIC_BASE_URL, CLAUDECODE, CLAUDE_CODE_*, …)
brain providers     [llama-cpp-local, deterministic-local]
external AI         NONE

initial   owner=WORKER work_state=ASSIGNED rev=1
tick 1    ADVANCED        WORKER -> TL   status=COMPLETE         provider=llama-cpp-local
tick 2    ADVANCED        TL -> QA       status=COMPLETE         provider=llama-cpp-local
tick 3    ADVANCED        QA -> PM       status=NEEDS_DECISION   provider=llama-cpp-local
tick 4    AWAITING_HUMAN  needs_decision QA

final     owner=QA work_state=NEEDS_DECISION delivery=DELIVERED rev=4
accepted  3 handoffs, all "local-" prefixed (Orbit-produced, not human-carried)
receipts  3 written, 3 accepted
tasks     WORKER COMPLETE, TL COMPLETE, QA NEEDS_DECISION — attempts=1 each

HUMAN COURIER STEPS : 0
EXTERNAL AI CALLS   : 0
```

**Every role was reasoned by the local model** — `provider=llama-cpp-local` on all three ticks.

Tick 3 is the most interesting result. The QA agent independently returned `NEEDS_DECISION`; the scheduler routed it to the registered `decisions` destination and surfaced `AWAITING_HUMAN`. That is a model exercising the blocker path on its own judgement, not a scripted fixture — and Orbit handling it correctly rather than advancing through it.

**Caveat, stated precisely.** Socket severing applies to the Python process. `llama-cli` is a separate local binary; it reads local weights and has no network code path, but the socket patch does not itself constrain a subprocess. The stronger claim available is that no vendor endpoint, credential or quota is involved anywhere in this run.

---

## 5. Correction: the "hardware limit" was my bug

My previous handoff reported that the local model could not run usefully on this hardware, and recommended a PM decision about switching models. **That conclusion was wrong and I want to be explicit about it.**

What actually happened:

```text
LlamaCppBrain invoked llama-cli WITHOUT closing stdin.
llama-cli therefore entered its interactive prompt loop and waited for input
that would never arrive: 782 KB of ">" prompts, 5 GB resident, no generation,
death only by timeout.
```

I read that as slow inference and reported a hardware constraint. The evidence was there — the earlier run produced 782 KB of output for a 24-token request — and I did not look at it closely enough before writing the conclusion.

The fix is one line plus a flag: `stdin=subprocess.DEVNULL`, and `-st` alongside `-no-cnv`. With it, the same model on the same CPU answers in **15.6 seconds**.

Pinned by `LLAMA-013` (stdin closed), `LLAMA-014` (single-turn flags present) and `LLAMA-015` (timeout always bounded), so this cannot silently regress.

Two further consequences worth recording:

- The runaway process was consuming 5 GB of RAM on a 16 GB machine. I killed it as soon as the diagnostic exposed it.
- My earlier statement that execution had been "declined" was also wrong — you corrected it, and it was the rejections that delayed finding this, not any refusal on your part.

---

## 6. Architecture-Style Disposition

**COMPATIBLE.**

- No shared contract changed — no manifest, envelope, receipt or state schema touched.
- No acceptance criterion changed. The floor was **raised** (51 → 131 tests), never lowered.
- `allowed_executor_operations` remains exactly `["PLACE_PACKET"]`; `place_packet.py` untouched. The typed local executor is a **standalone-kernel capability surface** and deliberately does not alter the workflow executor catalog — the two are separate authority systems.
- The standalone kernel is strictly additive. Deleting `standalone/` leaves the accepted system exactly as it was.
- Optional-provider abstraction holds both ways, now demonstrated in both directions: the local model answers when present, and the deterministic provider covered a real fault when it hung.

---

## 7. QA-Style Disposition

**QA_GO.** 131 pass, 2 skipped (symlink creation needs Developer Mode; Windows **junction** escape coverage supersedes it).

| Case | Covers |
| :--- | :--- |
| `OFFLINE-002` | severs socket creation, **then** runs the full chain |
| `BRAIN-002` | structural: brain package cannot import any durable-state type |
| `OFFLINE-005/006` | quota-exhausted provider falls back; no provider → typed BLOCKED |
| `AGENT-004/005/007` | restart no-duplicate; STOP halts; COMPLETE unreachable from brain output |
| `SCHED-003/004` | blocker and product decision route locally then surface |
| `LLAMA-008` | objective containing `& calc.exe --dangerously-skip-permissions -m C:/evil.gguf` lands inside exactly one argv element |
| `LLAMA-009` | model claiming `allowed_executor_operations` rejected by the contract gate |
| `LLAMA-013/014/015` | stdin closed, single-turn, bounded timeout |
| `EXEC-010..016` | traversal, POSIX/Windows absolute, UNC, backslash traversal all denied |
| `EXEC-017/018` | **real Windows directory-junction escape denied**, via `_winapi.CreateJunction` |
| `EXEC-030..036` | capability enforcement, gated ops refused, no generic command op exists |
| `EXEC-040..043` | size cap, non-UTF8, wrong-kind-of-path |

**Four defects found and fixed by self-review this burst:**

1. Scheduler reported `ADVANCED` for a gated transition the engine held at `APPROVAL_PENDING`. (`c956aff`)
2. `LlamaCppBrain.available()` consulted only the filesystem, so an injected runner was unreachable.
3. **The stdin hang** — §5.
4. **The executor's absolute-path check tested `is_absolute()` and `drive` but not `root`.** On Windows `/etc/passwd` is rooted but not absolute, and joining it onto the approved root silently discards everything below the drive letter — pathlib's absolute-join trap. It was still denied fail-closed via the reparse check, but for the wrong reason, and would have become a genuine escape had check ordering ever changed. Found by `EXEC-012`.

---

## 8. PM-Style Disposition

**Accepted:**

- standalone architecture: brain, agent runtime, scheduler, typed read-only executor;
- **real local reasoning** driving all three roles of a real work item, offline;
- no hard external dependency anywhere in Orbit core;
- optional-provider abstraction, proven in both directions;
- local model installed on `E:` with zero `C:` footprint;
- regression floor raised to 131.

**Not claimed:**

- that Phi-3.5-mini's *judgement quality* is sufficient for production review — it produced coherent, correctly-shaped answers in three roles, which is a mechanism result, not a quality benchmark;
- write/process/Git executor operations — declared and gated, not enabled;
- Claude/ChatGPT/Antigravity transport — checkpointed, unmerged;
- any change to workflow executor authority.

This meets the directive's **best case** for §19: Orbit boots offline, the brain interface exists, the agent runtime exists, the scheduler runs Worker/Review/QA roles, workflow state advances without external agents, and external providers are optional.

---

## 9. Remaining Gaps, Ranked by Leverage

**1. Reasoning quality is unmeasured.** The mechanism works; nothing yet tells you whether a 3.8B model's review is *good enough* to trust. The cheapest answer is a small eval set — a handful of work items with known-correct dispositions, scored against the local model. That also settles whether Qwen2.5-7B (Apache-2.0, 4.36 GB) is worth the partial CPU offload. ~2 hours.

**2. Objective intake is still human.** A human states the objective and bootstraps the work item; the chain from there is autonomous. Now genuinely unblocked, because objective → work-item is the first step needing real reasoning and there is now a real brain. ~1–2 hours.

**3. The executor is read-only.** Agents can read what they review but cannot write, run tests, or report VCS state. `RUN_APPROVED_TEST` is the highest-value next operation — it would let a local QA agent actually verify rather than assert — and needs an approved-executable registry plus an argument allowlist behind an Architecture/QA gate.

---

## 10. Safety Statement

Explicitly confirmed:

- **Workflow executor authority unchanged** — `allowed_executor_operations` is exactly `["PLACE_PACKET"]`; `place_packet.py` not modified.
- **The new executor grants only read-only operations**, root-confined, capability-gated, with no generic command operation. Every write/process/VCS operation is declared-but-refused with its required gate named.
- **Claude's machine privilege was not used to justify Orbit authority.** Enforced structurally: the brain package cannot reach durable state, a model result cannot grant a capability, and the executor refuses operations its role was not granted.
- **Download confined to `E:` as instructed.** `C:` free unchanged at 12.6 GB. No install into `C:`, no PATH or registry change, no service registered.
- **Host treated carefully.** Model runs are below-normal priority, thread-capped, timeout-bounded, stdin-closed. When the diagnostic exposed a 5 GB runaway process I killed it immediately.
- **No secrets touched.** `~/.claude/.credentials.json` never read, copied or referenced. Secret/canary scan PASS, 0 hits.
- **No shared branch moved.** `origin/main` `6928e5b`; `origin/integration` `d836bf7`. No force-push, no history rewrite, no merge to a shared branch.
- **No product, contract, or acceptance-criteria change.** Floor raised, never waived.

---

## 11. Next Recommended Action

**Action.** Decide whether to spend ~2 hours building a small local-reasoning eval set — a handful of work items with known-correct dispositions — to measure whether Phi-3.5-mini's judgement is good enough to trust for real review work. The mechanism is proven; quality is now the only open question, and every remaining decision (keep this model vs. move to Qwen2.5-7B, how much to delegate to local agents) depends on that measurement.

**Proposed Owner.** `Orbit PM Pair`.

---

## Final Status

```text
COMPLETE_WITH_PROGRESS
```

### Clean continuation point

`claude/m0-standalone-runtime-001` @ `0813f44`, pushed. 131 tests pass, 2 skipped, 0 release-blocking skips. The offline trial with real local reasoning is reproducible on demand. `claude/m0-wf-transport-burst-001` @ `8683d18` holds the agent-neutral transport seam, unmerged.

Orbit now boots, reasons, orchestrates and audits with no internet, no vendor API, no credentials and no external AI service. The directive's canonical invariant holds.
