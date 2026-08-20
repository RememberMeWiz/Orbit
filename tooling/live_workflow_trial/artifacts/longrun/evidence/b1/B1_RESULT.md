# B1 — first live zero-courier round trip

Work item `M0-WF-B1-LIVE-001`, 2026-08-20. The first time Orbit carried a real
piece of work from the PM conversation to a worker and back without the Product
Owner touching a file.

## Result

**Courier actions: 0.** The Product Owner did not copy, paste, save, attach or
carry anything. The only human contribution was the decision itself.

| step | outcome | detail |
| :--- | :--- | :--- |
| preflight | `READY` | 795 accessibility descendants, trusted package, flag present |
| wake_pm | `PM_WOKEN` | `pmreq-9ae12ea75546663186d7` posted into Orbit PM |
| await_directive | `DIRECTIVE_ACCEPTED` | `pmdir-20260820-1930-b1-live-001` |
| dispatch | `DISPATCHED` | to `windows-worker`, `SENT_UNCONFIRMED` |
| await_worker | `WORKER_RESPONDED` | complete after 27.5s, 7 polls |
| collect | `COLLECTED` | 6917 bytes, header-validated |
| report_to_pm | `PM_WOKEN` | `pmreq-78282b95f1705b74cfd4`, digest attached |

Artifact: `HANDOFF_M0-WF-B1-LIVE-001_WORKER_TO_ORBIT.md`
SHA-256: `c028b97cd5a2062cea2415598c65f69dff7db150debbf9da2fdd79b998e3a6d1`

PM was asked which role should take the work and answered `windows-worker` —
the registry slug, not the chat's display title "Windows Workflow". That is the
self-describing request working: PM had no way to know the slug otherwise, and
a guess would have failed closed.

## Two defects this found that no stubbed test could

Both were only reachable by running against the real application.

### Keystrokes were sent without checking which window was in front

`SendKeys` targets the foreground window, and a UIA `SetFocus` on a background
window does not make that window foreground. So the `Ctrl+A` / `Ctrl+V` pair
that stages a message performed select-all-and-replace in whatever was actually
in front. Live, the paste never reached the composer and staging verification
caught it — which is the only reason nothing was sent. That was luck.

Every keystroke now goes through `Send-KeysTo`, which raises the intended
window, re-checks the foreground immediately before sending, and returns false
rather than sending blind.

### Orbit read its own reply template as PM's decision

The request carries a reply template so PM knows the schema, which means the
transcript always contains an envelope-shaped block Orbit wrote itself. The
parser took the *first* marker, parsed that template, reported
`directive-missing:directive_id,action`, and left PM's correct answer unread
further down.

The transcript is now scanned newest-first, malformed candidates are skipped
rather than fatal, and any value still wearing its `<angle brackets>` is refused
as an unfilled template.

### A third, found while waiting on the worker

`surface_ready` required the Send button, which the app replaces with Stop for
as long as a response is streaming — so a *working* window looked broken exactly
when Orbit needed to watch it. Readiness now asks whether the transport control
exists in either form; whether it is safe to send is still a separate question,
answered by `response_state` at the point of sending.

## What the worker returned

Five findings on the C2 accessibility guard, four of them actionable, and two
of them real defects in the shipped implementation:

1. A non-empty tree with no composer is not necessarily an accessibility
   failure — it may be a sign-in screen, a modal, or a settings page. Reporting
   `accessibility-not-exposed` overclaims and sends the human to restart
   something that does not need restarting.
2. A locked Windows session cannot be repaired by restarting ChatGPT.
3. A trusted, correctly flagged process that momentarily has no window deserves
   the same settling period `accessibility-not-exposed` already gets.
4. When a process has no window *and* no flag, the missing flag is the decisive
   immutable cause; reporting `app-has-no-window` points at the wrong remedy.
5. `READY` should not be reported unless the executable path, the command line,
   the window handle and the observed composer all belong to the *same*
   process and window.

The worker explicitly declined to recommend loosening the no-kill constraint.

Files in this directory: the assignment Orbit delivered, the handoff the worker
returned, and the step journal.
