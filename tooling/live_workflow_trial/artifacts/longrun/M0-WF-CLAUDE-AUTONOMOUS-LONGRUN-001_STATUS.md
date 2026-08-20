# Orbit Long-Run Status — M0-WF-CLAUDE-AUTONOMOUS-LONGRUN-001

> Operational status for the unattended long run. Not a technical approval
> artifact. Updated and pushed after each green atomic phase.

| Field | Value |
| :--- | :--- |
| Updated | 2026-08-20T12:05Z |
| Branch | `claude/m0-autonomous-longrun-001` |
| Started from | `446e43af18445067a9bc227bb810ce17069929cf` (head of `claude/m0-wf-apprentice-002`) |
| HEAD | `0f4cc5c` |
| Current phase | **B — live zero-courier trial** |
| Current objective | B2 — second hop (architecture-tl), blocked on a human click |
| Blocker | **Architecture TL is showing "Continue with Work?" and exposes no Continue control to accessibility. A human must click it.** |

---

## Completed

### A1 — canonical header compatibility — DONE · `30253be`

The canonical parser unwraps exactly one full-value Markdown inline-code
wrapper, so ``- Work Item: `M0-WF-LIVE-003` `` validates identically to the
plain form. Closes the defect where a genuine worker handoff was collected off
the bridge, materialised and digested correctly, then rejected by the engine.

Implemented in `workflow/core/validation.py` only — the canonical parser — so
engine, reconciler and ChatGPT collector share one rule and cannot drift.

Guards: single full-value wrapper only; plain unchanged; unbalanced, nested,
doubled, multiple and interior backticks left verbatim; bare double-backtick
does not collapse to empty; filename/SHA-256/handoff-id/sequence/sender/
recipient and routing authority unchanged; duplicate critical headers still fail.

### A2 — durable exactly-once delivery — DONE · `25d07f0`

`standalone/bridge/delivery.py`. The hazard being removed: pressing Send is an
*external* effect, so if the process dies between the click and the receipt, a
naive ledger shows "no receipt" and a restart sends again.

Intent is therefore written to disk **before** actuation:

```text
PENDING_SEND → STAGED_VERIFIED → SEND_ACTUATED → SENT_UNCONFIRMED → DELIVERED
                                                  FAILED | AMBIGUOUS
```

A record found in `SEND_ACTUATED` at load time *is* the uncertain window, and is
reconciled to `AMBIGUOUS` on read — because a crash leaves no chance to run
cleanup, so whoever next opens the ledger must resolve it. `AMBIGUOUS` never
auto-resends.

**Defect found by its own tests:** `mark_failed` matched only `SEND_ACTUATED`,
but `get()` calls `load()`, which had already reconciled that to `AMBIGUOUS` —
so an in-process failure right after actuation fell through to the `FAILED`
branch, which is *retryable*. That silently converted an unsafe-to-retry state
into a resendable one: exactly the double-send this ledger exists to prevent.
Both post-actuation states now stay ambiguous.

### A3 — PM-supervised apprenticeship loop — DONE · `fc7f18f`

`standalone/bridge/orchestrator.py`. Wake PM, wait for a machine-checkable
directive, dispatch where PM said, wait for the worker, collect, report back.

Two rules carry the safety: PM decides routing (Orbit never picks a target, and
a target named only in prose is refused), and nothing external happens twice.
A *failed* post does not open a pending request — Orbit must never wait for an
answer to a question PM never received.

### C1 — restart and recovery — DONE · `f7399be`

Restart is covered as its own property rather than inferred from unit tests.
Each test rebuilds the loop from the same files, which is exactly what a
restarted process sees, and asserts that no restart duplicates an external
effect or drops a decision: a pending PM question survives, a consumed
directive stays consumed, a dispatch that already actuated stays `AMBIGUOUS`
instead of resending, a STOP taken before send leaves nothing to reconcile.

### C2 — accessibility runtime guard — DONE · `ced97b5`

The bridge only works when the renderer exposes a semantic tree, which is a
launch-time property: an app already running without
`--force-renderer-accessibility` cannot be persuaded to grow one. The guard
therefore has exactly two moves — start the app with the flag when it is not
running, or report precisely why the surface is unusable.

**It never closes, kills or restarts a running app.** The Product Owner may be
mid-conversation in that window, and an unattended process that ends a human's
session to unblock itself is a worse failure than staying blocked. Every
unusable-but-running case ends in `NEEDS_HUMAN_RESTART` carrying the remedy.
`launch_app` refuses outright if any ChatGPT process exists, and the driver
contains no `Stop-Process`, `taskkill`, `Kill` or `CloseMainWindow` — asserted
by test rather than left to review.

`app_state` separates *the flag was passed* (read from the command line) from
*the flag took effect* (measured by finding a composer in the tree). `launch_app`
resolves the executable from the installed package rather than a pinned path,
so an ordinary app update cannot silently disarm the guard.

### C3 — committed endpoint registry — DONE · `f7399be`

The five role chats PM actually addresses are committed configuration
(`standalone/bridge/orbit_endpoints.json`) rather than test fixtures. Adding a
role chat is a reviewable data change, and `enabled` is separate from
registration so a chat can be known without being writable.

Verified live against the running desktop app:

| endpoint | enabled | resolves to |
| :--- | :--- | :--- |
| `orbit-pm` | yes | Orbit PM |
| `windows-worker` | yes | Windows Workflow |
| `architecture-tl` | yes | Architecture TL |
| `qa-safety` | yes | QA TL |
| `product-research` | yes | Product Research |
| `android-worker` | **no** | denies `endpoint-disabled` |
| `memory-worker` | **no** | denies `endpoint-disabled` |

The two disabled endpoints deny even though both chats exist and are observed.

### B (runner) — the full cycle as one governed sequence — DONE · `328cf5a`

`standalone/bridge/roundtrip.py`. `Orbit PM → Orbit → Worker → Orbit → Orbit PM`
end to end, with the Product Owner's only involvement being the decision itself.

Each step declares what it must produce, and anything else ends the cycle where
it stands rather than improvising past it: a blocked surface posts nothing at
all, a directive for another work item never reaches a worker chat, a target PM
never registered is never focused, a worker that never finishes is never
collected from, and a missing artifact is never reported to PM as success.

Every step is journalled before and after, so an interrupted cycle can be read
afterwards to see how far it got, and each step stays individually resumable
through the same durable state the CLI verbs use.

### Operator CLI — `standalone/bridge/apprentice_cli.py`

```text
status   wake   poll   dispatch   await   collect   cycle   clear
```

Step-at-a-time by design: each verb is one invocation leaving durable state
behind, which makes a crash between two steps an ordinary restart rather than a
special case. Every verb that touches the app preflights through the
accessibility guard, so a dead surface is reported with its remedy instead of
failing deeper in as an opaque driver error. `--no-launch` withholds the launch
for supervised runs.

---

## Tests / native gates

```text
workflow     67 pass
standalone  295 pass   (2 skipped: symlink creation needs Developer Mode;
                        Windows junction coverage supersedes them)
native       14 pass
TOTAL       376 pass, 2 skipped, 0 release-blocking skips
```

---

## Live-trial result

### B1 — COMPLETE. Courier actions: 0.

Orbit carried a real piece of work from the PM conversation to a worker and
back without the Product Owner touching a file.

| step | outcome |
| :--- | :--- |
| wake_pm | `PM_WOKEN` · `pmreq-9ae12ea75546663186d7` |
| await_directive | `DIRECTIVE_ACCEPTED` · `pmdir-20260820-1930-b1-live-001` |
| dispatch | `DISPATCHED` → `windows-worker` |
| await_worker | `WORKER_RESPONDED` · 27.5s, 7 polls |
| collect | `COLLECTED` · 6917 bytes, header-validated |
| report_to_pm | `PM_WOKEN` · digest attached |

PM was asked which role should take the work and answered `windows-worker` —
the registry slug, not the chat's display title. Evidence in
`longrun/evidence/b1/`.

The worker returned five findings on the C2 guard, four actionable, two of them
real defects. All are now implemented (`bc46e6c`).

### B2 — second hop dispatched, then blocked

PM read the B1 result and directed the next hop to `architecture-tl`. Orbit
dispatched the assignment **with the worker's handoff attached as a real file**,
exercising the clipboard file-drop path live.

That conversation then entered a state Orbit cannot pass:

```text
Continue with Work?
This request requires creating and delivering a file artifact.
```

The composer is removed and **no Continue or Cancel control is exposed to
accessibility at all** — enumerating all 110 buttons in the window finds only
sidebar controls. Orbit did not click anything: granting that confirmation
starts real work and is a decision, not a mechanical step.

Reported to PM as `pmreq-68a8bdce7dd30a15fe1d`.

**To unblock: a human clicks Continue in the Architecture TL conversation.**
Orbit then resumes with `await --endpoint architecture-tl` and `collect`.

## Defects found live, all fixed

None of these were reachable from the stubbed tests.

1. **Keystrokes were sent without checking which window was in front** (`a7cd192`).
   `SendKeys` targets the foreground window and a UIA `SetFocus` on a background
   window does not make it foreground, so `Ctrl+A`/`Ctrl+V` performed
   select-all-and-replace in whatever *was* in front. All six keystroke sites
   now raise the intended window and re-check the foreground immediately before
   sending, or send nothing.

2. **Orbit read its own reply template as PM's decision** (`a7cd192`). The
   transcript is now scanned newest-first, malformed candidates are skipped, and
   any value still wearing its `<angle brackets>` is refused as unfilled.

3. **A streaming window looked broken** (`ae0e65c`). Send is replaced by Stop
   while a response streams, so readiness failed exactly when Orbit needed to
   watch a worker. Readiness now accepts either transport control.

4. **One stuck conversation stranded the whole app** (`0f4cc5c`). Three places
   measured app-wide health against whichever chat was on screen. Preflight now
   asks `drivable` rather than `ok`; `focus()` requires only the chat list
   before switching; and the post-switch header check is polled rather than
   raced against the outgoing conversation.

---

## Safety invariants still holding

```text
workflow executor catalog        ["PLACE_PACKET"]   unchanged
standalone write/process/Git     gated              unchanged
app termination capability       none               asserted by test
wrong-recipient sends            0
wrong-artifact substitution      0
duplicate workflow advancement   0
arbitrary command from handoff   0
secret leakage                   0
app confirmations auto-clicked   0
shared branches moved            none (main 6928e5b, integration 0813f44)
force-push / history rewrite     none
```

---

## Claude usage

Authorised to use the full remaining allowance. Consumed so far: moderate —
phases A1–A3, C1–C3, and the Phase B runner.
