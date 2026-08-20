ORBIT-B1-LIVE-004-QA-SAFETY

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-WF-B1-LIVE-001.
PM directive: pmdir-20260820-2034-b1-live-004.

Attached: the Architecture TL classification review from the previous hop
(SHA-256 97055f1b68eeb68e4c0ad81c05eddcbe3517e61ac5c08faf98fc7e76152b0f81).

## Task

**Try to break these claims.** Each is a safety property Orbit currently asserts
about itself. For each, either construct a concrete sequence of events under
which it fails, or state what evidence would be needed to trust it.

Assume an adversarial-but-realistic Windows desktop: the user alt-tabs, the app
updates mid-session, the machine sleeps, a second monitor is unplugged, the
process is killed, the network drops, two ChatGPT windows are open.

### Claim 1 — Orbit can never end a running ChatGPT session

Orbit may start the app only when no ChatGPT process exists. `launch_app`
enumerates processes first and refuses with `launch-refused-already-running` if
any exist. The driver source contains no `Stop-Process`, `taskkill`, `.Kill()`
or `CloseMainWindow`, asserted by a test that greps the driver. The guard class
has no method named kill/terminate/close_app/stop_app/restart.

### Claim 2 — no keystroke reaches a window that is not verifiably in front

All six keystroke sites go through one helper. It returns false unless the
intended window handle equals `GetForegroundWindow()` — checked once, then again
immediately before `SendKeys`, because the foreground can change in between.
Callers treat false as failure and send nothing.

The keystrokes in question are `Ctrl+A`, `Ctrl+V`, `Escape` and `Enter`.

### Claim 3 — Orbit never acts on a target named only in prose

A dispatch requires an `ORBIT_DIRECTIVE` envelope quoting the currently pending
`request_id`, naming the work item, and naming a `target_endpoint` that resolves
against a committed registry. Resolution checks: registered, `enabled=true`,
project scope, workflow scope, and that exactly one *observed* chat title folds
to the registered title. Ambiguity raises rather than picking. Each
`directive_id` is consumed once and thereafter inert.

The transcript is scanned newest-first because it accumulates; any value still
wearing `<angle brackets>` is refused as an unfilled template.

### Claim 4 — nothing external happens twice

Delivery states:

```text
PENDING_SEND -> STAGED_VERIFIED -> SEND_ACTUATED -> SENT_UNCONFIRMED -> DELIVERED
                                                    FAILED | AMBIGUOUS
```

Intent is written to disk *before* Send is pressed. A record found in
`SEND_ACTUATED` at load time is reconciled to `AMBIGUOUS`, which never
auto-resends and requires a human disposition. Any failure after actuation
resolves to `AMBIGUOUS`, never `FAILED` (which is retryable).

### Claim 5 — Orbit does not grant in-app confirmations

During this very work item, Architecture TL displayed "Continue with Work? This
request requires creating and delivering a file artifact" and removed its
composer. Orbit reported the blocker to PM and stopped. It did not click, and no
control for that prompt is exposed to accessibility in any case.

Orbit has a guarded geometry-click helper (last resort, refuses unless the
target app is the foreground window, always verifies a post-condition).

### Claim 6 — no secrets leave the machine

Orbit reads conversation transcripts and writes them to local state. It never
extracts cookies, tokens or session identifiers. The endpoint registry is
identity metadata only, and a test greps it for `token`, `cookie`, `password`,
`session_id`, `bearer`, `authorization`.

## What to return

A single downloadable Markdown file named exactly:

    HANDOFF_M0-WF-B1-LIVE-001_QA_TO_ORBIT.md

beginning with this header block, filled in:

```
# Safety Claim Adversarial Review

## Header
- Work Item: `M0-WF-B1-LIVE-001`
- From: `QA`
- To: `ORBIT`
- Status: `COMPLETE`
- Handoff ID: `M0-WF-B1-LIVE-001-0003`
- Sequence: `3`
```

Then, for each claim, a `### Claim N` section containing:

- **Verdict:** holds / holds conditionally / breaks
- **Attack:** the concrete sequence you tried, or the strongest one you can construct
- **Residual risk:** what remains true even if the claim holds
- **Required evidence:** what would have to be tested to trust it

Finish with `## Highest-Priority Gap` — the single thing you would fix first,
and why that one over the others.

Rank by exploitability on a real desktop, not by theoretical severity. Do not
propose giving Orbit termination or restart authority; that constraint is fixed.
Attach the file rather than pasting it as a chat message.
