# Orbit Long-Run Status — M0-WF-CLAUDE-AUTONOMOUS-LONGRUN-001

> Operational status for the unattended long run. Not a technical approval
> artifact. Updated and pushed after each green atomic phase.

| Field | Value |
| :--- | :--- |
| Updated | 2026-08-20T06:35Z |
| Branch | `claude/m0-autonomous-longrun-001` |
| Started from | `446e43af18445067a9bc227bb810ce17069929cf` (head of `claude/m0-wf-apprentice-002`) |
| HEAD | see latest commit |
| Current phase | **A — finish zero-courier core correctness** |
| Current objective | A3 — PM round-trip control, then Phase B live trial |
| Blocker | NONE |

---

## Completed

### A1 — canonical header compatibility (§7, `APPROVED_WITH_GUARDS`) — DONE

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

`30253be feat(validation): canonical header accepts inline-code wrapped scalars`

### A2 — durable exactly-once delivery — DONE

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

Wired into `ChatGptAdapter.deliver()`, so the ledger is enforced rather than
merely available: STOP is checked before any state is opened, the artifact
digest is recomputed at the last moment before actuation, and every
post-actuation failure resolves to `AMBIGUOUS` rather than `FAILED`.

**Defect found by its own tests:** `mark_failed` matched only `SEND_ACTUATED`,
but `get()` calls `load()`, which had already reconciled that to `AMBIGUOUS` —
so an in-process failure right after actuation fell through to the `FAILED`
branch, which is *retryable*. That silently converted an unsafe-to-retry state
into a resendable one: exactly the double-send this ledger exists to prevent.
Both post-actuation states now stay ambiguous.

27 `DLV` tests: lifecycle, six crash-point scenarios, payload-integrity,
work-item isolation, malformed-ledger, and seven `deliver()` integration tests
including *an ambiguous request must not touch the app at all* (asserted by the
driver call list being empty).

---

## Tests / native gates

```text
workflow     67 pass
standalone  157 pass   (2 skipped: symlink creation needs Developer Mode;
                        Windows junction coverage supersedes them)
native       14 pass
TOTAL       238 pass, 2 skipped, 0 release-blocking skips
```

---

## Live-trial result

None since A1. The bridge itself was proven live in the previous burst:
verified chat focus, staged-message readback, send, semantic completion
detection, clipboard file-drop attachment, and artifact collection to an exact
path with SHA-256 and header validation.

---

## Open defects

None known.

---

## Next action

**A3 — PM round-trip control**, then **Phase B1**: the first full real
Worker round trip (`Orbit PM → Orbit → Windows Worker → Orbit → Orbit PM`) with
Product Owner courier actions = 0, now that A1 makes a real returned handoff
validate and A2 makes the send exactly-once across a crash.

---

## Safety invariants still holding

```text
workflow executor catalog        ["PLACE_PACKET"]   unchanged
standalone write/process/Git     gated              unchanged
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
phases A1 and A2.
