# Orbit Long-Run Status — M0-WF-CLAUDE-AUTONOMOUS-LONGRUN-001

> Operational status for the unattended long run. Not a technical approval
> artifact. Updated and pushed after each green atomic phase.

| Field | Value |
| :--- | :--- |
| Updated | 2026-08-21T00:45Z |
| Branch | `claude/m0-autonomous-longrun-001` |
| Started from | `446e43af18445067a9bc227bb810ce17069929cf` (head of `claude/m0-wf-apprentice-002`) |
| HEAD | `ffe46cf` |
| Current phase | **B — live zero-courier trial** |
| Current objective | B4 — repeat the chain with no human UI clicks (addendum item D) |
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
standalone  330 pass   (2 skipped: symlink creation needs Developer Mode;
                        Windows junction coverage supersedes them)
native       14 pass
TOTAL       411 pass, 2 skipped, 0 release-blocking skips
```

---

## Live-trial result

### Full chain PROVEN — Worker -> TL -> QA -> PM

Three hops, three distinct endpoints, every one routed by a PM directive that
Orbit had to ask for. Evidence in `longrun/evidence/b1/`.

| hop | endpoint | directive | returned | bytes |
| :--- | :--- | :--- | :--- | ---: |
| 1 | `windows-worker` | `pmdir-...-b1-live-001` | accessibility guard review | 6917 |
| 2 | `architecture-tl` | `pmdir-...-b1-live-002` | classification review | 10041 |
| 3 | `qa-safety` | `pmdir-...-b1-live-004` | adversarial safety review | 13719 |

Hop 2 also delivered hop 1's handoff **as an attached file**, exercising the
clipboard file-drop path live.

Every returned handoff was materialised to Orbit's inbox, SHA-256 hashed and
header-validated before anything acted on it. PM chose each next hop after
reading the previous result — including routing to `architecture-tl` and then
`qa-safety` entirely on its own.

**Courier actions: 0**, except one class of human click described below.

### The one thing that needed a human, and why it no longer does

Twice the app interrupted a dispatch with:

```text
Continue with Work?
This request requires creating and delivering a file artifact.
```

It removes the composer and exposes **no** Continue or Cancel control to
accessibility — enumerating all 110 buttons in the window finds only sidebar
controls. Orbit reported the blocker to PM and stopped rather than
blind-clicking, which is correct but leaves the chain stalled.

The cause is not random: the prompt appears because the assignment asked for a
**downloadable file artifact**, and the app offers to escalate into a mode that
spends credits to produce one. For a plain text handoff that means paying for
something the conversation could have contained.

So the prompt is now avoided rather than answered. A worker returns its handoff
inline between `ORBIT_HANDOFF_BEGIN`/`ORBIT_HANDOFF_END` markers and Orbit reads
it from the transcript — no file card, no save dialog, no escalation offer, no
credits. Confirmed live: the same worker, given a no-file assignment, answered
directly with no prompt at all.

That path was designed twice. The first attempt asked for Markdown and failed
live with `artifact-missing-formal-header`, because the accessibility tree keeps
plain text and discards structure — `## Header` arrived as `Header` and the
bullet list under it disappeared entirely. Flat `key: value` lines survive
intact, which is exactly why the ORBIT_DIRECTIVE envelope has always worked over
the same channel, so the handoff now uses that shape and Orbit renders the
canonical Markdown locally.

### QA's adversarial verdict

Six safety claims, submitted for attack rather than confirmation:

| claim | verdict |
| :--- | :--- |
| 1 — cannot end a running session | breaks |
| 2 — no keystroke to a window not in front | breaks |
| 3 — never acts on a target named in prose | breaks |
| 4 — nothing external happens twice | holds conditionally |
| 5 — does not grant in-app confirmations | breaks |
| 6 — no secrets leave the machine | breaks |

Two are now closed:

**Claim 2 (QA's top-ranked gap).** The double foreground check narrows the
time-of-check/time-of-use window without ever closing it. The composer exposes a
writable `ValuePattern`, which is bound to the *element* rather than to whatever
holds focus, so message staging now uses no keystroke and no clipboard at all.
Verified before adoption that the editor genuinely registers it: the app's own
Send button transitions disabled -> enabled -> disabled tracking the set, and
that signal is now a second staging check alongside the text read-back.

**Claim 3.** Every check was field-level, so a well-formed envelope was accepted
regardless of author — and newest-first scanning made injection easier, not
harder. Directives are now read only from assistant turns, using the app's own
`ChatGPT said:` / `You said:` markers. A transcript with no markers yields no
directive at all, because absent provenance is none rather than weak.

Claims 1, 5 and 6 remain open and are recorded, not glossed. Claim 4's residual
is concurrency: the ledger prevents a restarted runner from resending, but two
concurrent runners are a separate problem needing a single-writer lock.

## Defects found live, all fixed

None of these were reachable from the stubbed tests.

1. **Keystrokes were sent without checking which window was in front**
   (`a7cd192`), later removed from message staging altogether (`31407bd`).
2. **Orbit read its own reply template as PM's decision** (`a7cd192`).
3. **A streaming window looked broken** (`ae0e65c`) — Send is replaced by Stop
   while a response streams, so readiness failed exactly when Orbit needed to
   watch a worker.
4. **One stuck conversation stranded the whole app** (`0f4cc5c`), including the
   PM chat Orbit would have used to report it.
5. **Markdown cannot survive the transcript channel** (`ffe46cf`).

## Reviews acted on

The workers reviewed Orbit, and Orbit changed:

* windows-worker found two real defects in the C2 guard shipped an hour earlier
  — a no-window process with no flag reported the wrong cause, and `READY` could
  be asserted without tying path, command line, window and composer to one
  process. Both fixed (`bc46e6c`).
* architecture-tl returned "sound with changes" and seven rule changes; six
  implemented (`96eb7dc`), the seventh deferred with its reason.
* qa-safety's two highest-ranked gaps are closed as above (`31407bd`, `f145dd4`).

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
paid work-mode escalations       0
keystrokes in message staging    0   (element-bound value write)
directives from non-assistant    0   (refused: provenance-unknown)
shared branches moved            none (main 6928e5b, integration 0813f44)
force-push / history rewrite     none
```

---

## Claude usage

Authorised to use the full remaining allowance. Consumed so far: moderate —
phases A1–A3, C1–C3, and the Phase B runner.
