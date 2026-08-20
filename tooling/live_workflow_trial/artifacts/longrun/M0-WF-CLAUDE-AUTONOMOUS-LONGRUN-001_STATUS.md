# Orbit Long-Run Status — M0-WF-CLAUDE-AUTONOMOUS-LONGRUN-001

> Operational status for the unattended long run. Not a technical approval
> artifact. Updated and pushed after each green atomic phase.

| Field | Value |
| :--- | :--- |
| Updated | 2026-08-20T06:17Z |
| Branch | `claude/m0-autonomous-longrun-001` |
| Started from | `446e43af18445067a9bc227bb810ce17069929cf` (head of `claude/m0-wf-apprentice-002`) |
| HEAD | `30253be` |
| Current phase | **A — finish zero-courier core correctness** |
| Current objective | A2 — durable exactly-once delivery lifecycle |
| Blocker | NONE |

---

## Completed since previous update

### A1 — canonical header compatibility (§7 ruling, `APPROVED_WITH_GUARDS`)

The canonical parser now unwraps exactly one full-value Markdown inline-code
wrapper, so a header written as ``- Work Item: `M0-WF-LIVE-003` `` validates
identically to the plain form.

This closes the defect reported at the end of the previous burst: a genuine
worker handoff was collected off the ChatGPT bridge, materialised to an exact
path and digested correctly — and then rejected by the engine, because every
real handoff uses backticks and every original fixture did not.

Normalisation lives in `workflow/core/validation.py` only — the canonical
parser. The engine, the reconciler and the ChatGPT collector all read that one
rule, so they cannot drift apart, and the collector needed no special case.

Guards implemented and tested exactly as specified:

- only a single wrapper around the entire trimmed scalar is removed;
- plain values unchanged;
- unbalanced, nested, doubled, multiple and interior backticks left verbatim,
  so a malformed header is never silently repaired into a *different* value;
- a bare double-backtick does not collapse to empty, which would hide a
  malformed critical field;
- filename identity, SHA-256 identity, handoff id, sequence, sender, recipient
  and routing authority all unchanged;
- duplicate critical headers still fail.

Commit: `30253be feat(validation): canonical header accepts inline-code wrapped scalars`

---

## Tests / native gates

```text
workflow     67 pass
standalone  130 pass   (2 skipped: symlink creation needs Developer Mode;
                        Windows junction coverage supersedes them)
native       14 pass
TOTAL       211 pass, 2 skipped, 0 release-blocking skips
```

New this phase: 16 `HDRS-001..016` regressions — the full guard matrix, direct
`.md` and ZIP-root `HANDOFF.md` acceptance through the real engine,
digest-is-of-the-bytes, and proof that a genuine work-item or sender mismatch is
still rejected when backticked.

The three earlier `HDR` tests pinned the pre-ruling behaviour and were updated to
the ruling, with the history kept in their docstring so the reversal stays
legible rather than looking like a test edited to fit.

---

## Live-trial result

None this phase — A1 is parser-level. The bridge itself was proven live in the
previous burst: verified chat focus, staged message readback, send, semantic
completion detection, file attachment by clipboard file-drop, and artifact
collection to an exact path with SHA-256.

---

## Open defects

None known.

---

## Next action

**A2 — durable exactly-once delivery.** Persist the real send lifecycle:

```text
PENDING_SEND → STAGED_VERIFIED → SEND_ACTUATED → SENT_UNCONFIRMED → DELIVERED
                                                  FAILED | AMBIGUOUS
```

so that a crash *after* actuation but *before* a durable receipt resolves to
`AMBIGUOUS` and never auto-resends. Today that window would look like "no
receipt" on restart, which is the one remaining way a duplicate send could
happen.

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

Authorised to use the full remaining allowance for this run. Consumed so far:
modest — Phase A1 only.
