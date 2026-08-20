# Orbit Long-Run Status — M0-WF-CLAUDE-AUTONOMOUS-LONGRUN-001

> Operational status for the unattended long run. Not a technical approval
> artifact. Updated and pushed after each green atomic phase.

| Field | Value |
| :--- | :--- |
| Updated | 2026-08-20T06:16:54Z |
| Branch | `claude/m0-autonomous-longrun-001` |
| Started from | `446e43af18445067a9bc227bb810ce17069929cf` (head of `claude/m0-wf-apprentice-002`) |
| HEAD | see latest commit on branch |
| Current phase | **A — finish zero-courier core correctness** |
| Current objective | A2: durable exactly-once delivery lifecycle |
| Blocker | NONE |

## Completed since previous update

**A1 — canonical header compatibility (§7 ruling, APPROVED_WITH_GUARDS).**
The canonical parser now unwraps exactly one full-value Markdown inline-code
wrapper, so a real Orbit handoff written as `- Work Item: \` validates
identically to the plain form. This closes the defect reported at the end of the
previous burst, where a genuine collected worker handoff was materialised and
digested correctly and then rejected by the engine.

Applied in `workflow/core/validation.py` (one place — the canonical parser), so
the engine, the reconciler and the ChatGPT collector all read the same rule and
cannot drift apart.

Guards implemented and tested exactly as specified:

- only a single wrapper around the entire trimmed scalar is removed;
- plain values unchanged;
- unbalanced, nested, doubled, multiple and interior backticks left verbatim —
  a malformed header is never silently repaired into a different value;
- `\` does not collapse to empty;
- filename identity, SHA-256 identity, handoff id, sequence, sender, recipient
  and routing authority all unchanged;
- duplicate critical headers still fail.

## Tests / native gates

```text
workflow    67 pass
standalone 130 pass  (2 skipped: symlink creation needs Developer Mode;
                      Windows junction coverage supersedes them)
native      14 pass
TOTAL      211 pass, 2 skipped, 0 release-blocking skips
```

New this phase: 16 `HDRS-001..016` regressions covering the guard matrix, plus
direct `.md` and ZIP-root `HANDOFF.md` acceptance through the real engine, and
proof that a genuine work-item/sender mismatch is still rejected when backticked.

## Live-trial result

None this phase (A1 is parser-level).

## Open defects

None known.

## Next action

A2 — persist the send lifecycle
(`PENDING_SEND → STAGED_VERIFIED → SEND_ACTUATED → SENT_UNCONFIRMED → DELIVERED`,
plus `FAILED` / `AMBIGUOUS`) so a crash after actuation becomes `AMBIGUOUS`
rather than an invisible resend risk.

## Safety invariants still holding

```text
workflow executor catalog        ["PLACE_PACKET"]   unchanged
standalone write/process/Git     gated              unchanged
wrong-recipient sends            0
wrong-artifact substitution      0
duplicate workflow advancement   0
secret leakage                   0
shared branches moved            none (main 6928e5b, integration 0813f44)
force-push / history rewrite     none
```

## Claude usage

Long run authorised to use the full remaining allowance. Consumed so far this
run: modest — Phase A1 only.
