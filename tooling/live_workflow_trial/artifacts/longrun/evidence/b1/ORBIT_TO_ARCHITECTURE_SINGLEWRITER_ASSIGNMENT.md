ORBIT-B1-LIVE-006-SINGLE-WRITER

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-WF-B1-LIVE-001.
PM directive: pmdir-20260821-0052-b1-live-006.

**Do not create or deliver any file. Answer in this conversation as plain chat.**

## Context

QA reviewed six Orbit safety claims. Five broke; one held conditionally. That
one is the delivery ledger:

```text
PENDING_SEND -> STAGED_VERIFIED -> SEND_ACTUATED -> SENT_UNCONFIRMED -> DELIVERED
                                                    FAILED | AMBIGUOUS
```

Intent is written to disk before Send is pressed. A record found in
`SEND_ACTUATED` at load time is reconciled to `AMBIGUOUS`, which never
auto-resends and requires a human disposition. Any failure after actuation
resolves to `AMBIGUOUS` rather than `FAILED`, because `FAILED` is retryable.

QA's residual: this proves a *restarted* runner will not resend, but not that
two *concurrent* runners cannot each actuate once. Both could load the same
record before either persists a transition, both stage, both press Send. It also
noted that pressing Enter exactly once does not prove the remote service cannot
duplicate the submission internally.

The ledger is a single JSON file written with an atomic replace
(`atomic_write_json`: write temp, then `os.replace`). There is no lock. Orbit is
a Windows program; runners are ordinary Python processes started from a CLI, and
may be started by a human, a scheduler, or a watchdog after an apparent hang.

## Task

Design the single-writer guarantee. Concretely:

1. **Mechanism.** What should Orbit use on Windows to guarantee that exactly one
   runner may transition a delivery record? Consider an exclusive file lock, a
   named mutex, a lock file with owner metadata, and a compare-and-swap on a
   revision number inside the record. Say which you would choose and why, and
   what each fails to protect against.

2. **Crash and stale ownership.** A holder that dies must not block Orbit
   forever, and a holder that is merely slow must not be evicted mid-actuation.
   How should ownership expire, and what evidence should the next runner require
   before taking over? Note that the dangerous case is takeover during the
   `SEND_ACTUATED` window.

3. **Scope.** Should the lock cover the whole ledger, one work item, or one
   delivery record? What breaks with each choice when Orbit is later supervising
   several work items at once?

4. **What it still will not prove.** Be explicit about the boundary. If the
   remote app can duplicate a submission internally, or a human presses Send in
   the same conversation, no local lock helps. State precisely what the claim
   should be narrowed to, in one sentence.

5. **Test strategy.** What must actually be run — not unit tests with a stubbed
   lock, but a real adversarial test — before "nothing external happens twice"
   may be stated without qualification? Include what to kill, when, and what to
   measure.

Rank by what a realistic Windows desktop can produce, not theoretical severity.
Do not propose giving Orbit process-termination authority over other runners;
that constraint is fixed.

## What to return

Write your answer **in this conversation** as plain chat, exactly in this form:

    ORBIT_HANDOFF_BEGIN HANDOFF_M0-WF-B1-LIVE-001_ARCHITECTURE_TO_ORBIT-3.md
    work_item: M0-WF-B1-LIVE-001
    from: ARCHITECTURE
    to: ORBIT-3
    status: COMPLETE
    handoff_id: M0-WF-B1-LIVE-001-0005
    sequence: 5
    ORBIT_HANDOFF_BODY
    ...your answer, numbered 1-5, then a final "Recommended Claim Wording" line...
    ORBIT_HANDOFF_END

Formatting rules, and the reason for each:

- The three marker lines and the six `key: value` lines must each be on their
  own line, at the start of the line, with **no** bullet, no numbering, no bold,
  no backticks and no code fence around them.
- Use exactly those six field names, lower case with underscores. Do not use
  Markdown headings or bullet lists for them.
- Everything after `ORBIT_HANDOFF_BODY` is free prose, formatted however you like.
- Write the marker lines exactly once each. Do not quote, echo or demonstrate
  them anywhere else in your message: Orbit requires exactly one eligible block
  and refuses ambiguity, so a second copy makes the whole answer uncollectable.

The reason is measured, not stylistic: this text reaches Orbit through the
Windows accessibility tree, which keeps plain text and discards structure. A
Markdown heading arrives with its `#` stripped and a bullet list under it can
disappear entirely. Flat `key: value` lines survive intact, so Orbit reads those
and renders the canonical Markdown handoff itself.
