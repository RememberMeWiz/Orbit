# Accessibility Guard Classification Review

## Header
- Work Item: `M0-WF-B1-LIVE-001`
- From: `ARCHITECTURE`
- To: `ORBIT`
- Status: `COMPLETE`
- Handoff ID: `M0-WF-B1-LIVE-001-0002`
- Sequence: `2`

## Verdict

**sound with changes** — the revised guard fixes the worker's main correlation and transient-state defects, but the trust/session ordering, `Document` discriminator, and two restart remedies are still too strong.

## Findings

### 1. Evaluation order: locked session versus untrusted path

The order should change.

If executable path and process identity were successfully observed from the selected process, `app-untrusted-path` should be evaluated **before** `interactive-session-unavailable`.

A locked or secure desktop prevents reliable UIA-tree observation, but it does not invalidate an already collected executable path. Trust of the selected process is a safety property independent of whether the interactive desktop is currently available. Reporting only `interactive-session-unavailable` can temporarily hide the stronger fact that Orbit selected a process outside the trusted package.

The correct precedence is therefore:

1. observation failed;
2. no process;
3. untrusted selected-process path;
4. multiple-window ambiguity;
5. interactive session unavailable;
6. remaining window/flag/UIA classification.

If the observation failure means the executable path itself was not obtained or cannot be trusted, row 1 still wins. The rule is not "trust always beats session state"; it is "evaluate any independently established trust failure before UIA availability failures."

The worker correctly identified that process path, launch flag, window, and UIA readiness must be correlated to one process/window. The new same-process observation rule fixes that earlier aggregation defect.

### 2. Row 6 versus row 7: immutable cause versus observed-first cause

For this specific pair, preferring `accessibility-flag-absent` is correct.

A trusted selected process with no window and no `--force-renderer-accessibility` flag already has a decisive launch-time defect. Waiting for a window cannot make that flag appear. Reporting `window-not-ready` first would describe a transient symptom while hiding the condition that actually determines the human remedy.

However, this should **not** become a general architecture rule that "immutable causes always beat observed-first causes." The correct rule is narrower:

> Prefer a cause over a symptom only when the cause is independently observed, causally decisive for the required remedy, and cannot be repaired by waiting for the symptom to change.

The worker's original finding supports this exact special case: no-window must not mask a decisive missing accessibility flag.

### 3. `web_content_present = Document` as discriminator

A single UIA `Document` control is **not** a sound discriminator between "renderer accessibility is functioning" and "this view is not a chat."

It is too weak in both directions:

- non-chat Electron/Chromium surfaces can expose a `Document`;
- sign-in, settings, first-run, modal, or update surfaces may expose web semantics without being a usable chat;
- a chat surface can temporarily lack the expected `Document` shape while the renderer is loading or rebuilding its accessibility subtree;
- the presence of one control says little about whether the semantic subtree is rich enough for reliable automation.

The worker explicitly warned that a non-chat view can expose a non-empty semantic UIA tree while lacking the ProseMirror composer, and that this must not be interpreted as renderer accessibility failure.

Use separate positive observations instead:

- `renderer_semantics_present`: a non-degenerate semantic web subtree is exposed under the selected window, based on multiple observable signals rather than a single `Document` node;
- `chat_surface_present`: one or more stable, read-only chat-surface landmarks are present;
- `accessibility_ready`: the ProseMirror composer is present.

Classification should then distinguish:

- composer present -> `READY / ok`;
- renderer semantics present + chat surface present + no composer -> `UNAVAILABLE / composer-not-present`;
- renderer semantics present + no chat surface -> `UNAVAILABLE / non-chat-surface` (human should navigate/sign in/dismiss blocking UI);
- flag present + window present + semantic subtree still absent/degenerate after settling retries -> `UNAVAILABLE / accessibility-not-exposed`.

The guard may only observe, so it should report what is positively established and avoid inferring "renderer dead" from absence of a single control type.

### 4. Multiple windowed instances

Refusing `READY` when more than one instance owns a window is correct **under the current selection rule**.

The new same-process correlation removes the worker's original false-READY risk caused by combining fields from different processes. But the target-selection rule is still "first process with a main window." With two windowed instances, "first" is not a stable identity contract. The selected process/window can change across observations, relaunches, or process enumeration order.

Because a `READY` classification is meaningful only if downstream accessibility work is bound to the same stable target, `multiple-instance-ambiguous` should continue to fail closed.

This can be relaxed later only if Orbit gains an explicit, persistent target binding such as `(pid, hwnd, executable identity)` or another stable instance identifier and all subsequent observation/action remains pinned to that exact target. Until then, row 5 is conservative but justified.

### 5. Wrong remedies and uncovered states

Two remedies should change.

First, `app-untrusted-path` should not use `NEEDS_HUMAN_RESTART`. An executable outside the trusted package proves a trust mismatch, not that restarting is the correct repair. The guard cannot know whether the user should exit an old install, choose another instance, finish an update, or investigate a foreign process.

Use:

```text
UNAVAILABLE / app-untrusted-path
remedy: verify the running ChatGPT instance/install before continuing
```

Second, row 11 should not use `NEEDS_HUMAN_RESTART / accessibility-not-exposed` merely because the flag is present, no composer exists, and no `Document` exists. With `Document` removed as the discriminator, that tuple is not strong enough to prove a restart is required. The worker already established that flag-present/no-composer can result from session state, startup, or a non-chat surface rather than a launch defect.

Use a non-destructive unavailable state after retries unless Orbit has positive evidence that the semantic renderer subtree should be present but is not:

```text
UNAVAILABLE / accessibility-not-exposed
remedy: restore a normal interactive ChatGPT surface; human may relaunch if necessary
```

The human may choose to relaunch, but the classifier should not encode restart as the uniquely established remedy.

One additional runtime state should be represented explicitly:

```text
window exists
flag present
renderer semantics present
chat surface absent
composer absent
```

This is a distinct **non-chat surface** state, not `composer-not-present` and not `accessibility-not-exposed`. Its remedy is navigation/sign-in/dismissal of blocking UI in the already-running app.

The fixed action boundary remains correct: Orbit may start ChatGPT only when no process exists; otherwise it reports and stops. No terminate, kill, close-window, or automated restart authority should be added.

## Required Changes

1. **Move trust evaluation ahead of session-lock evaluation when process identity/path were successfully observed.**

   Required precedence:

   ```text
   observation failed
   -> no process
   -> untrusted selected-process path
   -> multiple-window ambiguity
   -> interactive-session-unavailable
   -> remaining flag/window/UIA classification
   ```

2. **Keep row 6 before row 7, but codify the rule narrowly.**

   Prefer an observed cause over a transient symptom only when that cause independently determines the remedy and cannot resolve by waiting. Missing launch-time accessibility flag satisfies that rule; "immutable beats transient" is not a general classifier principle.

3. **Replace `web_content_present = any Document control` with richer observation.**

   Introduce at least:

   ```text
   renderer_semantics_present
   chat_surface_present
   accessibility_ready
   ```

   `renderer_semantics_present` must require a non-degenerate semantic subtree, not one `Document` node.

4. **Add an explicit non-chat-surface classification.**

   When renderer semantics are present but chat-surface landmarks and composer are absent:

   ```text
   UNAVAILABLE / non-chat-surface
   remedy: navigate/sign in/dismiss blocking UI in the existing app
   ```

5. **Retain fail-closed multiple-instance handling.**

   Keep:

   ```text
   >1 windowed instance
   -> UNAVAILABLE / multiple-instance-ambiguous
   ```

   until Orbit has a stable instance-binding contract used by every downstream accessibility operation.

6. **Change the untrusted-path remedy classification.**

   Replace:

   ```text
   NEEDS_HUMAN_RESTART / app-untrusted-path
   ```

   with:

   ```text
   UNAVAILABLE / app-untrusted-path
   ```

   The human remedy is to verify the running instance/install, not an architecture-prescribed restart.

7. **Change the flag-present accessibility-not-exposed remedy classification.**

   After the settling retries, use:

   ```text
   UNAVAILABLE / accessibility-not-exposed
   ```

   unless Orbit can positively establish a stronger restart-specific failure. Do not classify restart as required solely from absence of composer/web semantics.

8. **Preserve the fixed safety constraint.**

   Orbit may launch only when no ChatGPT process exists. For every running-process state it must report and stop. No app termination, forced restart, `Stop-Process`, `taskkill`, process `Kill`, or `CloseMainWindow` authority is permitted.
