# Two-lane supervision — live proof

PM's correction memo was explicit: *unit tests are not enough*, and multi-lane
supervision may not be called proven without a real ChatGPT Desktop trial with
at least two independent work items routed to different registered endpoints.

This is that trial. Nothing is mocked. The supervisor drove the real adapter,
switched real conversations, and pressed real Send buttons.

Run `123619`, 2026-08-21, 7 rounds, ~3.5 minutes.

## Result

| lane | work item | endpoint | request id | final |
| :--- | :--- | :--- | :--- | :--- |
| A | `M0-LANE-A-123619` | `windows-worker` | `pmreq-88ee2f0a7ccdcb7ae379` | **COMPLETED** |
| B | `M0-LANE-B-123619` | `architecture-tl` | `pmreq-18a09920906a8d77c3a4` | **COMPLETED** |

```text
  r1  LANE-A  PM_WOKEN                  AWAITING_PM_ROUTING
  r1  LANE-B  WAITING_FOR_TURN          INITIALIZED          <- window busy
  r2  LANE-A  DIRECTIVE_ACCEPTED        DIRECTIVE_ACCEPTED
  r2  LANE-B  PM_WOKEN                  AWAITING_PM_ROUTING  <- got its turn
  r3  LANE-A  DISPATCHED                AWAITING_WORKER
  r3  LANE-B  DIRECTIVE_ACCEPTED        DIRECTIVE_ACCEPTED
  r4  LANE-A  WORKER_IDLE_TRY_COLLECT   COLLECTING
  r4  LANE-B  DISPATCHED                AWAITING_WORKER
  r5  LANE-A  COLLECTED                 REPORTING_TO_PM
  r5  LANE-B  WORKER_IDLE_TRY_COLLECT   COLLECTING
  r6  LANE-A  REPORTED_TO_PM            COMPLETED
  r6  LANE-B  COLLECTED                 REPORTING_TO_PM
  r7  LANE-B  REPORTED_TO_PM            COMPLETED
```

## Each property PM asked for

| required | evidence |
| :--- | :--- |
| request IDs do not cross | two distinct ids; `crossed: false` |
| directives do not cross | A accepted at r2, B at r3, each quoting its own request |
| one lane may block while another advances | r1: B `WAITING_FOR_TURN` while A proceeded |
| handoffs remain work-item bound | each inbox holds only its own work item's handoff |
| no manual chat switching | every switch through `adapter.focus()` |
| no courier actions | 0 — both handoffs read from the transcript |
| no duplicate Send | 6 sends: 4 `orbit-pm` (2 wakes, 2 reports), 1 each worker |

Send counts were captured by wrapping the adapter, so they are independent of
what the supervisor believed it did.

## What it cost to get here

Five earlier runs failed, and each failure was a real defect that the 465-test
mocked suite passed straight over. That is the substance of PM's point about
live proof, so they are listed rather than tidied away.

**1. The worker wait could never complete.** The supervisor called
`wait_for_response(timeout=0.0)`. That routine concludes "complete" only after
seeing streaming and then idle hold, across several polls of its own; given a
zero timeout it returns on its first poll and always answers `timeout`. Not a
race — the only possible outcome, every time. The mock stubbed
`wait_for_response` to return complete, so it answered a question the code was
not asking.

**2. Contention was treated as failure.** Lane B woke PM one second after lane
A, found PM still streaming, and `send` correctly refused with
`response-in-progress`. The supervisor marked the lane terminally BLOCKED. The
window is shared, so finding it busy is the normal case; treating it as fatal
means the busier the system the more lanes die. Transient reasons now retry,
with a bounded count so a genuine stall still surfaces.

**3. Completion detection was edge-triggered.** After fixing (1), both lanes sat
with `saw_streaming: false` forever. The supervisor samples every ~30s because
it is sharing the window; a worker answering in twenty seconds starts and
finishes entirely between two samples. Now idle means "ask whether the handoff
is there", which is level-triggered and cannot be missed by sampling slowly. A
half-written answer is not mistaken for a finished one because collection
requires the closing marker.

**4. Consecutive assistant turns nested.** A turn ended only at the next *user*
marker, so two assistant turns in a row produced nested regions and anything
inside was counted once per enclosing turn — one handoff reported as
`transcript-handoff-ambiguous: 2 eligible blocks`. This one was mine, introduced
by the provenance hardening, and invisible offline because every offline
transcript alternated turns.

**5. The harness reused work-item identity.** Five runs against the same
`M0-LANE-A-001` left three handoffs with the same filename in one conversation,
which the collector correctly refused as ambiguous. Not a product defect: real
work items are unique, so the harness was fixed rather than the guard loosened.

## Not proven here

The trial demonstrated turn-taking under contention, which is what produced the
"one lane waits while another advances" evidence. It did **not** exercise an
explicit PM `HOLD` on one lane while the other ran to completion — that path has
unit coverage only, and should not be described as live-proven.
