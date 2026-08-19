# Orbit Handoff

## Header
- Work Item: `M0-WF-APPRENTICE-CLAUDE-BURST-002`
- From: `Claude Code Ultra / Opus 5`
- To: `Orbit PM Pair (Product Owner / Human PM & AI PM)`
- Status: `COMPLETE_WITH_PROGRESS`
- Handoff ID: `m0-wf-apprentice-claude-burst-002-claude-code-to-pm-001`
- Sequence: `1`
- Date: `2026-08-19`
- Contract Version(s): `orbit.workflow-contracts/0.1-draft` — no contract change requested

---

## 1. Executive Status

| Item | Value |
| :--- | :--- |
| Status | `COMPLETE_WITH_PROGRESS` |
| Branch | `claude/m0-wf-apprentice-002` |
| Baseline | `0813f444ab7568a4c588fe3241ef40f0aad252a1` (verified) |
| Foundation | `d0112927bd74fcbc0b142b68e8486e941f18faf8` (verified, branched from exactly this) |
| HEAD | `3915ce7` |
| Commits | 3 |
| Regression | **187 pass, 2 skipped** — 51 workflow + 124 standalone + 14 native |
| Real ChatGPT trial | **YES, partial** — outbound half ran live end to end |
| Product Owner courier actions | **0** |

### North-star test

> Can the Product Owner stay inside Orbit PM while Orbit itself interacts with the visible ChatGPT app?

**For the outbound half: yes, demonstrated live.**

```text
focus  : ok, header verified 'Orbit PM'
stage  : ok, 567 chars staged, composer readback 566, request_id verified
send   : ok, delivery_state=SENT_UNCONFIRMED
wait   : complete, 29.7s, 8 polls  (semantic streaming state, not a sleep)
```

Orbit selected the right conversation, proved it had the right one, composed a machine-generated `ORBIT_PM_REQUEST`, verified the exact text sitting in the composer, sent once, and detected the reply finishing. You touched nothing.

**Not yet done:** file attachment, inbound artifact collection, and therefore the full worker round-trip. §11 below is honest about why.

### One thing you should know first

During testing, **a partial `ORBIT_PM_REQUEST` was posted into your live Orbit PM chat that I did not intend to send.** Cause, fix and reasoning are in §5. Nothing was deleted, no other chat was touched, and the content was Orbit's own status envelope — but it was an unintended transmission into a real conversation and you should hear it from me directly rather than find it.

---

## 2. Accessibility Runtime and Selector Approach

Phase 0 re-verified independently before any input was injected:

```text
verdict     SEMANTIC_SURFACE_PRESENT
descendants 992      Document 1     Edit 2
Buttons 196          Lists 6        ListItems 51
```

The previous burst's blocker is genuinely gone. Landmarks discovered by read-only enumeration (Text nodes skipped, so no conversation content was collected):

| Purpose | Selector |
| :--- | :--- |
| composer | `Edit`, ClassName contains `ProseMirror` |
| attach | `Button`, Name `Add files and more` |
| send | `Button`, Name `Send` |
| response document | `Document`, AutomationId `RootWebArea` |
| project chat list | `List`, Name `Chats in Yong 2` |
| artifact card | `Button`, Name `Save <filename> as…` |

**Correction to the handoff's assumption:** the real role chat is **`Windows Workflow`**, not `windows-worker`. Registration uses the observed title.

**Selector durability.** ClassName is matched by substring, not equality, because ProseMirror appends state classes — the class becomes `ProseMirror ProseMirror-focused` the moment Orbit focuses the editor. Exact matching worked right up until Orbit touched it, then reported the composer missing while it sat in plain sight. No `radix-*` generated id is used as primary identity.

**Runtime guard.** The driver refuses any process whose path is not the trusted `OpenAI.Codex` package. It never kills or restarts a live session; an opaque surface returns a typed denial for you to decide on.

---

## 3. Registered Endpoint Identities

The app exposes no per-conversation AutomationId, so identity is **level 3** of §7's preference order — exact registered title + project chat list + verification anchor — and I am naming the level rather than implying a stronger one.

```text
orbit-pm          "Orbit PM"          PM      anchor: active-chat header
windows-workflow  "Windows Workflow"  WORKER  anchor: active-chat header
architecture-tl   "Architecture TL"   TL      anchor: active-chat header
qa-tl             "QA TL"             QA      anchor: active-chat header
```

**Ambiguity control that matters concretely.** `Orbit PM` appears **twice** in the window — once in the chat list and once as the active-chat header. Reading titles from the whole window would make every send look ambiguous and fail closed forever. Titles are therefore read **only** from inside the configured project list, which yields exactly the seven Orbit role chats and nothing from other projects.

Live verification, all three focus attempts confirmed by reading the header back:

```text
focus windows-workflow -> header='Windows Workflow'  verified
focus architecture-tl  -> header='Architecture TL'   verified
focus orbit-pm         -> header='Orbit PM'          verified
focus not-registered-chat -> endpoint-not-registered
focus android-worker      -> endpoint-not-registered   (a real chat, deliberately unregistered)
```

---

## 4. Outbound Transport

**Implemented and demonstrated live** for message delivery. Commit `3915ce7`.

Sequence, with every gate before Send:

1. `await_surface` — poll until composer/send/attach exist, never a fixed sleep
2. resolve endpoint from the registry by `endpoint_id`
3. focus, then **verify by reading the active-chat header back**
4. stage via clipboard paste
5. **read the composer back and require the request_id to be present**
6. **re-resolve the endpoint and re-read the header immediately before pressing Send**
7. press Send — requires exactly one *enabled* Send button and refuses if a stop control is present

Step 6 is the one that earns its keep: if the conversation changes between focus and send, the result is `destination-changed-before-send` and nothing is transmitted.

### Exactly-once posture — stated honestly

`PENDING_SEND / SENT_UNCONFIRMED / DELIVERED / FAILED / AMBIGUOUS` are declared, and a successful press returns **`SENT_UNCONFIRMED`**, never `DELIVERED` — a pressed button is not proof of delivery. Driver-level failures return `FAILED`.

**What is not yet built:** the durable pre-send receipt that would let a crash between press and persistence be reconciled as `AMBIGUOUS`. Today that window would look like "no receipt" on restart. `CHAT-013/014` are covered at the decision level in tests but not by durable persistence, and I am not claiming otherwise.

---

## 5. Correction: an unintended send into your live PM chat

`set_message` originally typed the body with `SendKeys`.

In this app **Enter submits**, and `SendKeys` turns every newline into an Enter press. Staging a multi-line message therefore **transmitted it line by line during typing**, before any verification gate could run. One partial `ORBIT_PM_REQUEST` reached the live Orbit PM conversation this way.

The staged-verification gate *did* work — it refused to proceed, reporting `staged-message-verification-failed` because the composer no longer held the token. But the send had already happened during staging, which is the wrong place for it to be possible at all. A gate after the dangerous step is not a gate.

**Fix.** Staging now goes through the clipboard: save previous clipboard → set text → select-all → paste → restore clipboard. Pasting inserts literal content and cannot submit. The keystroke-typing path is deleted entirely, with the reason recorded in the driver so it is not reintroduced.

**Second fix from the same investigation.** ProseMirror commits a paste to the accessibility tree asynchronously, so a single immediate readback legitimately missed content already present. `stage_message` now polls the readback to a deadline and still fails closed if the token never appears — the same principle as `await_surface`, applied to staging.

I found this by checking the live transcript for the request_id after the gate fired, rather than assuming the refusal meant nothing had happened.

---

## 6. Response Completion Mechanism

Read from the app's own state, never a timer:

```text
stop control present            -> streaming
Send present, no stop control   -> idle
```

Completion requires **streaming observed, then idle sustained for 6 seconds**. Two failure modes this specifically avoids:

- the app briefly shows no stop control between thinking and streaming, so a single idle sample is not proof of completion;
- idle *before* any reply starts is not a finished reply (`CHAT-018b`).

Losing the accessibility tree mid-wait returns `error`, not a retry loop (`A11Y-004`).

Live: `complete` after 29.7s across 8 polls.

---

## 7. Inbound Artifact Collection — enumeration only

The mechanism exists and is enumerable. Live, Orbit listed four saveable artifacts from the transcript, including `HANDOFF_M0-WF-APPRENTICE-CLAUDE-BURST-001_CLAUDE-CODE_TO_PM.md`.

`find_expected_artifact` requires **exactly one** card matching the expected filename: zero → `artifact-not-present`, more than one → `artifact-ambiguous`. Neighbouring files are never substituted.

**Not implemented:** actually materialising the bytes. That needs `Save <file> as…` → Windows file dialog automation → a fixed Orbit bridge inbox → SHA-256 → the existing handoff validator. The validation half is already solved by accepted code; what is missing is the dialog automation and the local-path correlation.

---

## 8. Orbit PM Bridge

Wired to the real conversation. Orbit emits a fenced, unmistakably machine-generated request:

```text
ORBIT_PM_REQUEST
version: 0.1
request_id: pmreq-3c9ed74b18b297831728
work_item: M0-WF-APPRENTICE-CLAUDE-BURST-002
current_owner: CLAUDE
reason: apprentice-bridge-online
safe_actions: DISPATCH_TO_ROLE, HOLD, STOP
awaiting: ORBIT_DIRECTIVE
```

Directive protections carried forward unchanged from the accepted foundation and still green: stale `request_id` inert, wrong work item inert, duplicate `directive_id` inert, prose without an envelope inert, no pending request means nothing executes, pending request survives restart.

**To direct Orbit**, reply in Orbit PM with an `ORBIT_DIRECTIVE` envelope quoting that `request_id`.

---

## 9. Teaching Trace

Schema, persistence, redaction and the no-silent-promotion rule are unchanged from the accepted foundation and still green (`TRACE-001..006`).

**Captured this burst: nothing.** No PM directive was executed, so there was no supervised decision to record. Writing a trace for a loop that did not complete would be fabricated training data.

---

## 10. Architecture and QA Disposition

**Architecture — COMPATIBLE.** `standalone/bridge` is imported by nothing in Orbit core; the scheduler, agent runtime, brain and workflow engine have no reference to it. Delete the package and the offline local-reasoning loop is unaffected. Workflow executor catalog unchanged: `["PLACE_PACKET"]`. Standalone write/process/Git operations remain gated.

**GUI authority boundary.** Seven typed chat operations, eight allowlisted driver operations, none accepting a coordinate, selector, key sequence, or executable path. Python never builds PowerShell source — it passes an operation name from a fixed allowlist plus a JSON parameter object. No `CLICK`, `TYPE_ARBITRARY_KEYS`, `RUN_GUI_SCRIPT`, `CONTROL_ANY_WINDOW`. No screen-perception fallback was built; UIA covered everything attempted.

**QA — QA_GO for what shipped.** 187 pass, 2 skipped (symlink tests needing Developer Mode; junction coverage supersedes them).

| Area | Status |
| :--- | :--- |
| `A11Y-001/003/004/005` surface present, opaque, lost mid-task, app absent | PASS |
| `CHAT-001/003/005` focus verified, header disagreement, unregistered | PASS |
| `CHAT-011/011b` unverified staged message blocks send | PASS |
| `CHAT-012` destination changed between focus and send | PASS |
| `CHAT-014` pressed Send reports SENT_UNCONFIRMED, never DELIVERED | PASS |
| `CHAT-015/016` streaming blocks send and staging; driver failure → FAILED | PASS |
| `CHAT-017/018/018b` streaming ≠ complete; sustained idle required | PASS |
| `CHAT-020/021/022/027` exactly one artifact, else fail closed | PASS |
| `UI-006/007` driver ops allowlisted, none accept coordinates | PASS |
| `PM-001..009`, `TRACE-001..006`, `CHAT-002/004` (foundation) | PASS |
| `CHAT-010/013` digest-bound send, durable crash reconciliation | **NOT IMPLEMENTED** |
| `CHAT-023/024/025/026` inbound validation | **NOT REACHED** — no collection yet |

---

## 11. Real Trial — outbound half only

**Ran:** Orbit → Orbit PM, live, message delivery, zero courier actions.

**Did not run:** the full `PM directive → role chat → attach → send → wait → collect → report` loop, because file attachment and artifact materialisation are not built. Attempting a worker round-trip without them would have meant hand-waving the file step, which is the entire point of the exercise.

Product Owner manual actions during what did run: **download 0, upload 0, drag/drop 0, chat switching 0, polling 0.**

---

## 12. Remaining Gaps, Ranked

**1. File attachment.** `Add files and more` is present and invokable, but the flow behind it (menu → file picker → dialog automation → confirm the staged filename in the UI) is unbuilt. This is the single thing between here and a real worker round-trip, and `CHAT-011`'s attachment form — *verify the attachment UI shows the expected filename before sending* — is exactly what makes it non-trivial.

**2. Artifact materialisation.** `Save <file> as…` cards are enumerable and disambiguated; turning one into bytes at a known path in a fixed bridge inbox needs the same dialog automation as (1), plus before/after correlation so no neighbouring Download can be mistaken for the result.

**3. Durable pre-send receipt.** Without persisting intent before pressing Send, a crash in that window cannot be reconciled as `AMBIGUOUS`. The states are declared; the persistence is not wired. This matters more once attachments are involved, because a duplicate send would then carry a file.

---

## 13. Safety Statement

- **No credential or session-token scraping.** No cookie, token store, browser storage or credential surface was read. Text nodes were skipped during discovery so conversation content was not collected.
- **No generic desktop executor.** Eight allowlisted driver operations, refusing any process outside the trusted `OpenAI.Codex` package.
- **No coordinates anywhere.** No operation accepts one; no prompt or handoff can supply one.
- **No app was killed or restarted.** An opaque surface returns a typed denial for you to decide on.
- **One unintended transmission occurred** into the live Orbit PM chat, disclosed in §5, cause fixed and pinned by the removal of the keystroke path.
- **Clipboard is saved and restored** around staging; it is global state and I did not want to silently clobber it.
- **No shared branch moved.** `origin/main` `6928e5b`, `origin/integration` `0813f44`. No force-push, no history rewrite, no merge.
- **No silent contract or product change.** Regression floor raised 164 → 187, never lowered.
- **Workflow executor catalog unchanged:** `["PLACE_PACKET"]`. Standalone write/process/Git operations remain gated.
- **No teaching trace promoted to policy** — structurally impossible.

---

## 14. Next Recommended Action

**Action.** Reply in Orbit PM with an `ORBIT_DIRECTIVE` envelope quoting `request_id: pmreq-3c9ed74b18b297831728`, action `HOLD`. That exercises the inbound half of the PM control loop against a real message you wrote — proving Orbit reads your directive, matches it to the pending request, and refuses stale or malformed ones — and it costs you one message.

The next *implementation* step is file attachment (§12.1), but the directive round-trip is worth confirming first, because everything downstream is gated on Orbit correctly obeying you.

**Proposed Owner.** `Orbit PM Pair`.

---

## Final Status

```text
COMPLETE_WITH_PROGRESS
```

`claude/m0-wf-apprentice-002` @ `3915ce7`, 187 pass / 2 skipped. The outbound courier works live and the Product Owner carried nothing. The inbound file half is unbuilt and not claimed.
