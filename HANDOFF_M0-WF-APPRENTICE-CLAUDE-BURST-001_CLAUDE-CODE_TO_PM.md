# Orbit Handoff

## Header
- Work Item: `M0-WF-APPRENTICE-CLAUDE-BURST-001`
- From: `Claude Code Ultra / Opus 5`
- To: `Orbit PM Pair (Product Owner / Human PM & AI PM)`
- Status: `NEEDS_DECISION`
- Handoff ID: `m0-wf-apprentice-claude-burst-001-claude-code-to-pm-001`
- Sequence: `1`
- Date: `2026-08-19`
- Contract Version(s): `orbit.workflow-contracts/0.1-draft` — no contract change requested

---

## 1. Executive Status

| Item | Value |
| :--- | :--- |
| Status | `NEEDS_DECISION` |
| Branch | `claude/m0-wf-apprentice-001` |
| Baseline | `0813f444ab7568a4c588fe3241ef40f0aad252a1` (verified match) |
| HEAD | `d011292` |
| Commits | 1 |
| Regression | **164 pass, 2 skipped** — 51 workflow + 101 standalone + 14 native gates |
| Verified from | clean `git archive` export, native validation PASS, secret scan PASS |
| Real ChatGPT apprenticeship trial | **NO — not attempted** |
| Product Owner courier actions | N/A (no trial ran) |

### The north-star test

> Can the Product Owner stay in the Orbit PM chat while Orbit carries the files?

**Not on this host, and not for a reason I can engineer around.** The ChatGPT desktop app exposes no accessibility surface at all — no composer, no send control, no attachment card, no conversation list. Every operation the mission requires would be blind coordinate clicking against a window Orbit cannot read.

Two of the mandated safety gates become *unsatisfiable* rather than merely difficult, which is what makes this a stop rather than a hard problem:

- **`CHAT-002`** (two similarly named chats must fail closed) requires reading the chat title. There is no chat title in the tree.
- **`CHAT-011`** (attachment UI shows wrong filename ⇒ no send) requires reading the attachment card. There is no attachment card in the tree.

A coordinate bot could *appear* to pass both by asserting on its own assumptions. It would be sending real handoffs to a chat it cannot identify. Per §22 I stopped with evidence rather than building it.

**What did land:** everything the apprenticeship loop needs that is not the GUI itself — endpoint identity with fail-closed resolution, the PM control envelope with replay protection, teaching traces, and a runnable diagnostic that re-tests the blocker in one command.

---

## 2. ChatGPT App Recon

### Identity

```text
package     OpenAI.Codex_26.803.10989.0_x64__2p2nqsd0c76g0   (MSIX / Store)
executable  C:\Program Files\WindowsApps\...\app\ChatGPT.exe
processes   10 (Electron main + renderers); 1 has a MainWindowTitle
window      class Chrome_WidgetWin_1, FrameworkId Win32  -> Electron/Chromium
```

### Accessibility surface — the blocking finding

**UI Automation:**

```text
descendants : 12
control mix : Pane x9, Button x3
buttons     : Minimize (aid=view_1), Restore (aid=view_3), Close (aid=view_4)
Edit        : 0
Document    : 0
```

**MSAA (independent API, same answer):**

```text
root        : name='ChatGPT' role=16 (ROLE_SYSTEM_WINDOW) childCount=2
children    : 2 unnamed windows, no content
```

**Activation attempted and failed:**

```text
WM_GETOBJECT(OBJID_CLIENT)         -> returned 49273 (window answers a11y queries)
AccessibleObjectFromWindow         -> hr=0, IAccessible ACQUIRED
UIA descendants after acquisition  -> still 12, rechecked at 1s/3s/5s and again at 6s
```

That is the documented Chromium accessibility activation path, and the renderer tree did not populate. This is consistent with modern Chromium's progressive/targeted accessibility: the shell answers, the web contents stay dark.

### Required controls — none available

| Control | Needed for | Present |
| :--- | :--- | :--- |
| conversation_list | select the registered chat | **no** |
| chat_title | verify the right chat (`CHAT-002`) | **no** |
| message_composer | send a bounded message | **no** |
| attach_control | attach the artifact | **no** |
| send_control | send | **no** |
| response_stream | distinguish streaming from complete | **no** |
| attachment_card | confirm the filename before send (`CHAT-011`), collect the result | **no** |

### Other surfaces found

- **`codex://` protocol handler** registered (`windows.protocol`, under `HKCU\SOFTWARE\Classes\codex`). Undocumented grammar — it may focus the app, but there is no known parameter form for "open conversation X and attach file Y", and guessing at a protocol grammar to route real handoffs is not safe.
- **File type association is `.skill` only.** Handoff `.md` / `.zip` artifacts have no association, so the shell "open with" route does not reach a chat either.
- `codex-computer-use.exe` ships inside the package. I did not touch it: driving OpenAI's own computer-use agent to operate its own UI is well outside this handoff's authority.

### Unstable areas

Not applicable — nothing was automated. The instability is categorical, not intermittent: the surface is absent, not flaky.

---

## 3. Endpoint Registry — implemented

`standalone/bridge/registry.py`. Explicit registration only; nothing is ever inferred.

Identity model: `endpoint_id`, `role_id`, `app`, `conversation_identity`, `display_title`, `project_scope`, `workflow_scope`, `enabled`, `verification_anchor`, plus a derived `identity_digest`.

Resolution is deny-by-default and takes the **observed** conversation titles from whatever discovery the host provides:

- endpoint not registered ⇒ `endpoint-not-registered`
- endpoint disabled ⇒ `endpoint-disabled`
- project/workflow scope mismatch ⇒ refused
- registered title not observed ⇒ `endpoint-not-observed` (a renamed or closed chat is never sent to)
- **more than one observed chat folds to the registered title ⇒ `endpoint-ambiguous-observed`**

Titles compare *folded* (case, spacing and punctuation removed) deliberately: "Orbit PM" and "orbit-pm" are treated as possibly-the-same-chat and therefore ambiguous, rather than as a clean miss. Erring toward ambiguity is the safe direction.

Ambiguous registrations are refused at construction, so a registry file cannot contain a latent collision.

**Live endpoints registered for test: none.** Registration requires a stable `conversation_identity`, which the app does not expose. The mechanism is complete and tested; it has nothing trustworthy to register yet.

---

## 4. Outbound Transport — NOT IMPLEMENTED

Blocked. No composer, attach control, or send control exists to drive.

Beyond "hard", the mandated gate `CHAT-011` — *verify the attachment UI shows the expected filename before sending* — cannot be satisfied at all without an accessible attachment card. Implementing send without it means Orbit would attach something it cannot see, to a chat it cannot name.

The `PENDING_SEND / SENT_UNCONFIRMED / DELIVERED / FAILED / AMBIGUOUS` lifecycle is declared in `bridge/contracts.py` so the exactly-once design is recorded, but no adapter drives it.

**No tests.** Green tests against a stub adapter would assert that my stub behaves, not that the real risk — right file, wrong chat — is controlled. That would be worse than no tests, because it would read as coverage.

---

## 5. Inbound Collection — NOT IMPLEMENTED

Blocked for the same reason: no message stream to detect completion, no file card to collect from.

Worth recording for the next attempt: the *validation* half is already solved and reusable. Once bytes reach a local path, the existing `WorkflowEngine` validator already enforces work-item binding, sender, sequence, replay, and digest — `CHAT-023/024/025` are covered by accepted code the moment collection exists. What is missing is only the mechanism that gets a file from the chat onto disk with a known path.

---

## 6. Orbit PM Bridge — implemented

`standalone/bridge/pm_envelope.py`. This is the piece that makes "PM says where to hand off" safe, and it works today.

PM speaks prose; **prose is not authority**. A directive executes only inside a delimited envelope:

```text
ORBIT_DIRECTIVE
version: 0.1
request_id: <the currently pending request>
directive_id: <unique>
work_item: <exact work item>
action: DISPATCH_TO_ROLE | COLLECT_RESULT | HOLD | STOP | ABANDON_REQUEST
target_endpoint: <registered endpoint or none>
artifact_id: <expected artifact or none>
notes: <bounded human-readable instruction>
```

Protections, all tested:

- must quote the **currently pending** `request_id` — a stale directive scrolled up in the chat is inert (`PM-002`)
- must name the work item — a decision about one item cannot move another (`PM-003`)
- `directive_id` consumed once, then inert (`PM-005`)
- `action` outside the allowlist refused even inside a valid envelope (`PM-007`)
- prose without an envelope never executes, including text that merely mentions the marker (`PM-004`)
- no pending request ⇒ nothing executes (`PM-006`)
- pending request survives restart (`PM-009`)

`PMRequest.render()` emits one message that is both human-readable and machine-parseable, ending `awaiting: ORBIT_DIRECTIVE`.

---

## 7. Teaching Trace — implemented

`standalone/bridge/teaching.py`. Append-only JSONL, work-item bound, with secret redaction on write (`TRACE-003` asserts a session token never reaches disk).

`condition_digest` fingerprints (work item, owner, work state, reason), so "equivalent conditions" means something checkable rather than vibes — `TRACE-006` confirms decisions under differing reasons do not aggregate.

**No silent rule promotion.** `propose_promotion()` reports repeated PM decisions as `status: PROPOSAL_ONLY` with `requires: explicit PM/Architecture approval`. There is deliberately **no `promote()` method**, and `TRACE-005` asserts that no `promote` / `activate_policy` / `make_autonomous` / `auto_continue` attribute exists. Nothing in Orbit reads these proposals to decide behaviour.

**Captured during a trial: nothing.** No trial ran.

---

## 8. Real Trial — did not run

No PM directive was executed, no chat was addressed, no artifact was sent or collected. Running one would have required the coordinate automation this handoff forbids.

Product Owner courier actions: **N/A**.

---

## 9. Architecture-Style Disposition

**COMPATIBLE.**

- **Why this remains an optional adapter:** `standalone/bridge` is imported by nothing in the core. The scheduler, agent runtime, brain and workflow engine have no reference to it. Delete the package and Orbit's offline local-reasoning loop is unaffected.
- **Why Orbit core remains standalone:** unchanged. Core still makes zero network calls and needs no credentials. The bridge adds no runtime dependency — and today it adds no runtime capability either.
- **GUI authority boundary:** none was added. `CHAT_OPERATIONS` contains seven semantic operations and no `CLICK`, `TYPE_ARBITRARY_KEYS`, `RUN_GUI_SCRIPT`, `CONTROL_ANY_WINDOW` or `BROWSE_ANY_APP` (`UI-003` asserts this). No request field accepts a coordinate, selector, key sequence, or executable path (`UI-002`). The only code that touches the app is a read-only diagnostic that enumerates control structure and never injects input.
- Workflow executor catalog unchanged: `["PLACE_PACKET"]`. Standalone executor write/process/Git operations remain gated.

---

## 10. QA-Style Disposition

**QA_GO** for what shipped. **NOT CLAIMED** for transport, which does not exist.

| Case | Status |
| :--- | :--- |
| `CHAT-001` exact registered chat selected | PASS |
| `CHAT-002` similarly named second chat ⇒ fail closed | PASS |
| `CHAT-003` renamed/missing endpoint ⇒ no send | PASS |
| `CHAT-004` wrong project/workflow anchor ⇒ no send | PASS |
| `CHAT-005` unregistered chat cannot be selected from prose | PASS |
| `CHAT-006/007/008` disabled endpoint, ambiguous registration, no secrets persisted | PASS |
| `CHAT-010..027` outbound/inbound | **NOT TESTED — no adapter exists** |
| `PM-001..009` directive authority, staleness, replay, prose, restart | PASS |
| `UI-001..005` prose cannot name destinations, supply selectors, or control apps | PASS |
| `DIAG-001..004` feasibility verdicts incl. probe failure | PASS |
| `TRACE-001..006` append, redaction, no promotion API | PASS |
| Existing floor (replay/digest/path/restart/junction) | PASS |
| Native Windows gates | 14/14 PASS |
| Reconciler smoke | PASS |
| Secret/canary scan | PASS, 0 hits across 20 files |

Clean-export verified: `51 + 101 + 14 = 166` collected, **164 pass, 2 skipped**. The 2 skips are symlink-creation tests needing Developer Mode; Windows junction escape coverage supersedes them and passes.

---

## 11. Remaining Gaps, Ranked by Leverage

**1. No semantic UI surface on the ChatGPT desktop app.** Everything else in this mission is downstream. Two bounded options exist and both need your decision — see §13.

**2. No stable `conversation_identity` to register.** Even granted an accessible UI, the registry needs something durable to bind an endpoint to. A window title is weak: renaming a chat silently breaks the binding, and two similar names are indistinguishable. Whatever activation path is chosen, the first thing to check is whether it exposes a per-conversation identifier.

**3. Collection depends on a local-path correlation that does not yet exist.** Validation is already solved — the accepted engine enforces work item, sender, sequence, replay and digest the moment bytes land on disk. What is missing is a deterministic way to learn *which* file a chat just produced without ingesting neighbouring Downloads. A before/after watched-directory correlation would work but needs its own adversarial pass (`CHAT-026/027`).

---

## 12. Safety Statement

Explicitly confirmed:

- **No credential or session-token scraping.** No browser or app credential store was read. `~/.claude/.credentials.json` untouched. The registry persists identity metadata only, and `CHAT-008` asserts no token/secret/password/cookie string reaches the file.
- **No generic desktop executor.** Seven typed chat operations, none of them a control primitive. `UI-003` asserts the forbidden names are absent.
- **No coordinates exposed to workflow prose.** No request field carries a coordinate, selector, key sequence, or executable path (`UI-002`).
- **No input injected into any application.** The only interaction was read-only structure enumeration.
- **No shared branch moved.** `origin/main` `6928e5b`, `origin/integration` `0813f44`. No force-push, no history rewrite, no merge.
- **No silent contract or product change.** Regression floor raised (131 → 164 pass), never lowered.
- **Workflow executor catalog unchanged:** `["PLACE_PACKET"]`.
- **Standalone write/process/Git operations remain gated.**
- **No teaching trace promoted to policy** — structurally impossible, `TRACE-005`.

---

## 13. Next Recommended Action

**Action.** Decide how Orbit should reach the role chats, given that the ChatGPT desktop app cannot be driven semantically. Three options, in my order of preference:

**(a) Try enabling Chromium renderer accessibility — cheapest test, needs your machine.**
Restart ChatGPT with `--force-renderer-accessibility`, or enable a system assistive-technology flag, then run the committed diagnostic:

```bash
python -m standalone.bridge.diagnostics
```

If it reports `SEMANTIC_SURFACE_PRESENT`, the entire transport mission unblocks and the next burst is ordinary implementation. I did not try this myself because it means restarting an app holding your live conversations. **~5 minutes of your time, and it settles the question.**

**(b) Change the channel.** If the desktop app stays opaque, the roles do not have to live in it. A filesystem-mediated channel — the one Orbit already validates natively — would make this problem disappear entirely. This is a product decision about where worker conversations happen.

**(c) Authorise coordinate automation.** I do not recommend it. It cannot satisfy `CHAT-002` or `CHAT-011`, which means Orbit would be sending real handoffs to a chat it cannot verify. If you want it anyway, it needs an explicit Architecture/QA gate, not my judgement.

**Proposed Owner.** `Orbit PM Pair` — with (a) worth doing before anything else, because it is five minutes and it decides the other two.

---

## Final Status

```text
NEEDS_DECISION
```

The primary mission is blocked by an external constraint I cannot engineer around without authority I was explicitly denied. Per §18, the only viable implementations require either a change to your machine's accessibility configuration or generic GUI automation — both reserved to you.

Everything implementable without that landed and is green: endpoint identity with fail-closed resolution, PM control authority with replay protection, teaching traces that cannot self-promote, and a diagnostic that turns this blocker from folklore into a one-command check that will report `SEMANTIC_SURFACE_PRESENT` the day it stops being true.

`claude/m0-wf-apprentice-001` @ `d011292`, 164 pass / 2 skipped, verified from clean export.
