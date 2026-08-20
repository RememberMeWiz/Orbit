ORBIT-B1-LIVE-007-CONCURRENCY-HARNESS

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-WF-B1-LIVE-001.
PM directive: pmdir-20260821-0152-b1-live-007.

**Do not create or deliver any file. Answer in this conversation as plain chat.**

## Context

Architecture has specified Orbit's single-writer guarantee: a Windows named
mutex protecting the whole delivery ledger, acquired before reading a record for
transition, held through every durable transition and through Send actuation,
released only after the post-actuation state is persisted. Deliberately no
time-based lease: a slow-but-alive holder inside the actuation window must never
be declared stale, and Windows already supplies safe expiry by marking the mutex
abandoned when the owning process dies. A waiter that times out reports
`writer-busy` and stops; a timeout never grants takeover.

QA's standing objection is that unit tests with a stubbed lock prove nothing
here. The claim being defended is narrow:

> Orbit guarantees at-most-once local Send actuation per delivery record among
> participating Orbit runners on the same Windows installation.

## Task

Specify the adversarial test harness that would actually justify that sentence,
runnable on an ordinary Windows desktop with no special hardware.

1. **Processes.** How many real Orbit runner processes, started how, racing on
   what? Say what makes the race tight rather than theoretical — the two
   processes must genuinely contend, not merely run one after the other.

2. **Kill points.** Exactly where should a runner be killed, and with what
   (graceful vs `TerminateProcess`)? The interesting boundaries are: after the
   durable `SEND_ACTUATED` write but before Send; between Send and the
   `SENT_UNCONFIRMED` write; and while holding the mutex.

3. **Instrumentation.** Send actuation is the thing being counted, so how is it
   counted independently of Orbit's own belief about what it did? Assume the
   real app is not available in the harness and a substitute must stand in for
   the composer and Send button.

4. **Pass condition.** State it as a measurable inequality over a stated number
   of trials, not as "no duplicates observed". Include what result should count
   as a failure even if no duplicate send occurred — for example a deadlock, an
   abandoned-mutex takeover that skipped reloading from disk, or a record left
   in a state no runner will ever resolve.

5. **What the harness still cannot show.** Be explicit, and say which residual
   risks belong in the claim's wording rather than in the test.

Rank by what a realistic Windows desktop can produce. Do not propose giving
Orbit authority to terminate other runners; that constraint is fixed.

## What to return

Write your answer **in this conversation** as plain chat, exactly in this form:

    ORBIT_HANDOFF_BEGIN HANDOFF_M0-WF-B1-LIVE-001_WORKER_TO_ORBIT-4.md
    work_item: M0-WF-B1-LIVE-001
    from: WORKER
    to: ORBIT-4
    status: COMPLETE
    handoff_id: M0-WF-B1-LIVE-001-0006
    sequence: 6
    ORBIT_HANDOFF_BODY
    ...your answer, numbered 1-5...
    ORBIT_HANDOFF_END

Formatting rules, and the reason for each:

- The three marker lines and the six `key: value` lines must each be on their
  own line, at the start of the line, with **no** bullet, no numbering, no bold,
  no backticks and no code fence around them.
- Use exactly those six field names, lower case with underscores. Do not use
  Markdown headings or bullet lists for them.
- Everything after `ORBIT_HANDOFF_BODY` is free prose, formatted however you like.
- Write the marker lines exactly once each. Do not quote, echo or demonstrate
  them anywhere else: Orbit requires exactly one eligible block and refuses
  ambiguity, so a second copy makes the whole answer uncollectable.

The reason is measured, not stylistic: this text reaches Orbit through the
Windows accessibility tree, which keeps plain text and discards structure. A
Markdown heading arrives with its `#` stripped and a bullet list under it can
disappear entirely. Flat `key: value` lines survive intact, so Orbit reads those
and renders the canonical Markdown handoff itself.
