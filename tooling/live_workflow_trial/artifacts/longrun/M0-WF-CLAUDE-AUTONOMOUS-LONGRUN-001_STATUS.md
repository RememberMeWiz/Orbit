# Orbit Long-Run Status — M0-WF-CLAUDE-AUTONOMOUS-LONGRUN-001

> Operational status for the unattended long run. Not a technical approval
> artifact. Updated and pushed after each green atomic phase.

| Field | Value |
| :--- | :--- |
| Updated | 2026-08-20T11:25Z |
| Branch | `claude/m0-autonomous-longrun-001` |
| Started from | `446e43af18445067a9bc227bb810ce17069929cf` (head of `claude/m0-wf-apprentice-002`) |
| HEAD | `328cf5a` |
| Current phase | **B — live zero-courier trial** |
| Current objective | B1 — first real Worker round trip with courier actions = 0 |
| Blocker | NONE |

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
standalone  251 pass   (2 skipped: symlink creation needs Developer Mode;
                        Windows junction coverage supersedes them)
native       14 pass
TOTAL       332 pass, 2 skipped, 0 release-blocking skips
```

---

## Live-trial result

**C2 and C3 verified live this run** against the running desktop app:

- `app_state` reports flag / trusted path / readiness over 793 accessibility
  descendants
- `launch_app` refuses with `launch-refused-already-running` — the never-kill
  guard proven live, not merely unit-tested
- `AccessibilityGuard.ensure()` returns `READY` without touching the session
- all five enabled endpoints resolve to exactly one observed chat; both
  disabled endpoints deny

**B1 not yet run live.** The runner and its offline proof are complete; the
live cycle requires a real PM directive in the Orbit PM conversation.

---

## Open defects

None known.

---

## Next action

**B1** — first full real Worker round trip
(`Orbit PM → Orbit → Windows Worker → Orbit → Orbit PM`) with Product Owner
courier actions = 0, via `apprentice_cli … cycle`.

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
shared branches moved            none (main 6928e5b, integration 0813f44)
force-push / history rewrite     none
```

---

## Claude usage

Authorised to use the full remaining allowance. Consumed so far: moderate —
phases A1–A3, C1–C3, and the Phase B runner.
