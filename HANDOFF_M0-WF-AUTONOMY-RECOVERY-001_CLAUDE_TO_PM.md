# HANDOFF: M0-WF-AUTONOMY-RECOVERY-001 — Autonomy Recovery and Always-On Supervisor

## Header
- **Work Item**: `M0-WF-AUTONOMY-RECOVERY-001`
- **From**: `Claude Code Ultra / Opus 5`
- **To**: `Orbit PM Pair / Product Owner`
- **Final Status**: `COMPLETE_WITH_PROGRESS`
- **Date**: `2026-08-22`
- **Branch**: `claude/m0-operator-reconcile-001`
- **Starting SHA**: `703574fe221057ce69da4dc8eab83b94a6fe2ecd`
- **Final SHA**: `fce9690`
- **main**: `6928e5bb46981e308c29838a85accfa476c78ea8` — UNCHANGED
- **integration**: `0813f444ab7568a4c588fe3241ef40f0aad252a1` — UNCHANGED
- **Tests**: 498 passed, 2 skipped

---

## 1. Root cause

The memo identified one cause. There were **three**, and the one it named was
the least damaging.

### 1a. Durable state lived inside a virtualized package cache

Orbit runs under the Microsoft Store build of Python, which redirects writes to
`%LOCALAPPDATA%` and `%APPDATA%` into that package's private LocalCache.
Measured on this host:

```text
LOCALAPPDATA  C:\Users\louis\AppData\Local
              -> ...\Packages\PythonSoftwareFoundation.Python.3.12_...\LocalCache\Local
APPDATA       C:\Users\louis\AppData\Roaming
              -> ...\Packages\PythonSoftwareFoundation.Python.3.12_...\LocalCache\Roaming
USERPROFILE   C:\Users\louis    -> itself
```

Every lane and ledger was therefore somewhere no other Python could see. Two
processes configured with the identical path still could not see each other's
work, and unlike an in-memory cache **restarting does not fix this**. A Python
reset or reinstall would have deleted all Orbit state silently.

This is why the state looked empty from outside while `orbit lanes` listed four.

### 1b. The supervisor could not discover lanes created after it started

As the memo said: `load_lanes()` ran once at construction and `cycle_all()`
iterated that snapshot forever.

### 1c. A malformed lane record was silently reinitialised and overwritten

```python
except Exception:
    pass
rec = LaneRecord(work_item=self.work_item, objective="")
self.save_record(rec)
```

An unparseable record was replaced with a blank one — in the constructor, so
merely looking at a lane destroyed the evidence. Worse than losing the file: a
lane whose JSON was truncated by a crash mid-delivery returned as a fresh
`INITIALIZED` lane with no pending request and no accepted directive, so the
supervisor would wake PM and **dispatch the same work again**.

### Contributing, all fixed

- `orbit work` returned 0 for every outcome including `WAKE_FAILED`, which is
  precisely how the arms-only receipt recorded three lanes as registered while
  no PM request had been posted.
- The dispatched assignment was `Assignment for W: <objective>` — no markers, no
  filename, no field shape. A worker could not have answered collectably.
- `expected_handoff` was empty, so collection fell back to `HANDOFF_<work>.md`,
  which cannot match the handoff grammar. Guaranteed block.
- The overnight runner **exited** when the surface was not drivable at startup.
  The three lanes were registered while the surface read `NEEDS_HUMAN_RESTART`.
- `endpoint-not-observed` was terminal, but the project chat list is
  recency-ordered and capped, so a registered chat drops off it whenever other
  conversations are used — including by Orbit itself.

---

## 2. Commits

```text
42433fd  fix(operator): state root, lane discovery, and a lane record that erased itself
9bd25a8  feat(operator): always-on supervisor contract, and an assignment that can be answered
c1f0214  fix(operator): spawn overnight with its real flag name
a487ed4  docs(longrun): autonomy recovery, supervisor live and advancing
2f5f5d2  fix(operator): a chat scrolled off the list is not a dead lane
```

Earlier on this branch, from the R2 reconciliation and multi-lane proof:
`4ae8f05`, `41eb3ad`, `9f8286b`, `fc326df`, `e13c938`, `1753273`, `a1a4c54`,
`8e7ea8a`, `703574f`.

---

## 3. Supervisor disposition

| | |
| :--- | :--- |
| Old PID | `76112`, created 2026-08-21 19:26:44 |
| Disposition | Stopped, after verifying preconditions |
| Preconditions checked | no ledger in `SEND_ACTUATED` or `AMBIGUOUS`; no delivery mutex held; full lane snapshot taken to `pre-restart-snapshot-20260822-070913` |
| ChatGPT | **untouched** — 11 processes still running after the stop |
| Current PID | `265580` (windowed console) |
| Running code fingerprint | `47f8655ccf6989b0`, equal to checkout |
| Heartbeat | `C:\Users\louis\.orbit\state\supervisor.heartbeat.json` |
| State root | `C:\Users\louis\.orbit\state` — verified not redirected |

The old process was terminated rather than drained because it predated the drain
protocol. Everything since uses `orbit supervisor drain`, which asks the process
to finish its critical section and exit.

---

## 4. Live evidence

### Lane discovery without restart — the headline requirement

A lane was created from a **second process** while the supervisor was running:

```text
orbit work --work-item M0-DISCOVERY-PROBE-001 ...
  -> lane_count observed by the running supervisor: 4 -> 5
```

No restart. The old supervisor could not have seen it at all.

### Three local-first lanes moving

```text
ARCH  PM_WOKEN -> DIRECTIVE_ACCEPTED -> DISPATCHED -> AWAITING_WORKER
COST  PM_WOKEN -> DIRECTIVE_ACCEPTED -> DISPATCHED -> COLLECTING
OPS   PM_WOKEN -> AWAITING_PM_ROUTING
```

| lane | request id | directive id | endpoint | preauthorized | match |
| :--- | :--- | :--- | :--- | :--- | :--- |
| ARCH | `pmreq-9fe72af8e7add64cc99a` | `pmdir-20260822-0642-local-first-arch-001` | `architecture-tl` | `architecture-tl` | OK |
| COST | `pmreq-1f72a674e57843f005f7` | `pmdir-20260822-0644-local-first-cost-001` | `product-research` | `product-research` | OK |
| OPS | `pmreq-f79e0fdfec28e2c6e19a` | pending | — | `windows-worker` | pending |

Both directives arrived through the normal governed envelope from PM's own
assistant turn. **Nothing was injected**: the preauthorized table was not needed
as an input, because PM routed to exactly those endpoints unprompted.

### Drain and restart with no duplicate Send

```text
delivery records BEFORE drain   : 8
orbit supervisor drain          -> OVERNIGHT_DRAINED, process exited itself
orbit supervisor ensure-running -> new pid, heartbeat fresh
delivery records AFTER restart  : 8
```

All lanes resumed in place; COST advanced to COLLECTING after the restart.

### Zero-tolerance outcomes

| outcome | result |
| :--- | :--- |
| new lane stranded until restart | none — discovered in one poll |
| duplicate Send | none — 8 records before and after restart |
| wrong directive consumed | none |
| wrong endpoint focused | none — routing matched preauthorization |
| new lane resets an existing lane | none |
| malformed lane crashes supervisor | none — held aside, others keep moving |
| stale process reported as current | none — fingerprint mismatch reports OUTDATED |
| ChatGPT terminated by Orbit | none |
| workflow STOP silently cleared | none — drain is a separate file |
| Product Owner courier action | none |

---

## 5. What is not proven

Stated plainly rather than folded into the above.

- **The three local-first lanes have not yet completed a full round trip.** Two
  are dispatched and one is collecting at the time of writing. The supervisor is
  running and will carry them; the completed cycle is not yet evidence.
- **`orbit supervisor` has no Windows Task Scheduler entry.** §6 marks that
  optional and I did not add a persistence mechanism that survives logoff.
- **Explicit PM `HOLD` on one lane while another completes** remains unit-tested
  only, as recorded in the previous handoff.
- The heartbeat's single-instance guarantee currently rests on `ensure-running`
  refusing to start a second process. It does not yet hold an OS mutex for the
  supervisor role itself, so two supervisors started by other means would not be
  prevented by the kernel the way delivery actuation is.

---

## 6. Product Owner note

Orbit's state root moved to `C:\Users\louis\.orbit\state`. If you would rather it
lived on `E:`, set `ORBIT_STATE_ROOT` and run `orbit migrate-state`; the resolved
path is printed by `orbit status` and redirection is reported rather than
tolerated.

The supervisor now runs in its own console window, so it can be watched directly
rather than only through a log file.

```bash
orbit supervisor status
orbit supervisor ensure-running --window
orbit supervisor drain
```
