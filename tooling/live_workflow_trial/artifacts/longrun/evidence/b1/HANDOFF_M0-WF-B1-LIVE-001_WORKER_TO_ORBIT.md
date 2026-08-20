# Accessibility Guard Review

## Header
- Work Item: `M0-WF-B1-LIVE-001`
- From: `WORKER`
- To: `ORBIT`
- Status: `COMPLETE`
- Handoff ID: `M0-WF-B1-LIVE-001-0001`
- Sequence: `1`

## Findings

### 1. Semantic accessibility is present, but the current window is not a chat-composer view

- **Runtime state:** ChatGPT is running from the trusted package, has a main window, was launched with `--force-renderer-accessibility`, and exposes a non-empty UIA descendant tree, but no ProseMirror composer is present because the visible surface is a sign-in screen, update/first-run surface, settings page, modal, or another non-chat view.
- **Guard currently concludes:** `NEEDS_HUMAN_RESTART` / `accessibility-not-exposed`.
- **Why that is wrong or incomplete:** The observation already says the semantic UIA tree is exposed. Absence of a ProseMirror composer is not sufficient to conclude that renderer accessibility failed. Restarting may not change anything if the user simply needs to sign in, dismiss a modal, or navigate back to a conversation.
- **What it should conclude instead:** Report a non-restart unavailable/human-action state, for example `UNAVAILABLE` / `composer-not-present`, with a remedy such as restoring/navigating the existing app to a chat composer. Do not terminate or restart the running app.

### 2. Interactive Windows session is locked or otherwise not exposing the normal desktop

- **Runtime state:** ChatGPT is running from the trusted package, is correctly launched with the accessibility flag, and may still have a main window, but the Windows session is locked, on a secure desktop, or otherwise temporarily prevents the normal UIA tree from being observed. Depending on the driver, this can look like `accessibility_ready = false` with zero/few descendants, or the observation itself may fail.
- **Guard currently concludes:** If the observation succeeds but no composer is visible, `NEEDS_HUMAN_RESTART` / `accessibility-not-exposed`; if the driver fails, `UNAVAILABLE` with the driver reason.
- **Why that is wrong or incomplete:** A locked or non-interactive desktop is not a launch-flag failure. Restarting ChatGPT cannot repair the Windows session state and would violate the intended safety posture if treated as the remedy.
- **What it should conclude instead:** When the driver can identify this condition, report `UNAVAILABLE` / `interactive-session-unavailable` (or an equivalent driver reason) and tell the human to unlock/restore the interactive session. If the guard cannot observe session state, it should not assert that restart is the required remedy from the same `flag present + no composer` tuple.

### 3. Correctly flagged app is still starting, restoring, or between windows

- **Runtime state:** A trusted ChatGPT process exists with `--force-renderer-accessibility`, but it temporarily has no main window because the app is still starting, restoring after an update, recreating its Electron window, or transitioning between windows.
- **Guard currently concludes:** Immediately `NEEDS_HUMAN_RESTART` / `app-has-no-window`.
- **Why that is wrong or incomplete:** The immutable launch-time requirement is already satisfied. A main window can appear moments later without restarting the process. The guard already acknowledges startup races for `accessibility-not-exposed`, but does not give the analogous no-window state any settling period.
- **What it should conclude instead:** Re-observe this state briefly when the trusted process has the accessibility flag. If a window appears, classify the resulting state normally. If the condition persists, report a non-destructive unavailable/human-action state such as `UNAVAILABLE` / `window-not-ready` rather than claiming that restart is necessarily required.

### 4. No-window state masks a decisive missing accessibility flag

- **Runtime state:** A single trusted ChatGPT process is running, has no main window, and its command line does not contain `--force-renderer-accessibility`.
- **Guard currently concludes:** `NEEDS_HUMAN_RESTART` / `app-has-no-window`, because the no-window rule is evaluated before the no-composer/flag-absent rule.
- **Why that is wrong or incomplete:** For this state, the missing flag is already the decisive launch-time defect. Even if the window appears later, the process still cannot acquire the flag in place.
- **What it should conclude instead:** `NEEDS_HUMAN_RESTART` / `accessibility-flag-absent`. The action remains report-and-stop; the point is to report the correct immutable cause and remedy.

### 5. Multiple ChatGPT processes/windows can collapse into an observation whose fields do not describe the same instance

- **Runtime state:** More than one ChatGPT process exists, for example during a Store updater relaunch, multiple profiles/windows, or a stale background instance. One process can be trusted and flagged while another owns the window; one window can expose a composer while another process is unflagged or outside the currently installed package path.
- **Guard currently concludes:** The table classifies one aggregate set of `running`, `windowed`, `trusted_path`, `accessibility_flag`, and `accessibility_ready` values. As described, it does not state that path, command line, window handle, and composer readiness are correlated to one specific process/window.
- **Why that is wrong or incomplete:** With mixed instances, the same aggregate booleans can be assembled from different processes. That can produce a false `READY`, or a restart reason that belongs to the wrong instance. This is especially plausible while the Store updater is replacing/relaunching the app or when more than one profile/window exists.
- **What it should conclude instead:** Do not report `READY` unless the trusted executable path, selected main window, and observed composer are tied to the same process/window, with the launch flag evaluated for that same owning process. If multiple running instances cannot be correlated unambiguously, report a blocked/non-destructive state such as `UNAVAILABLE` / `multiple-instance-ambiguous`, explain which instances were observed, and stop. The guard still must not close, kill, or restart any running process.

## Overall Assessment

The two permitted actions are appropriately conservative: launch only when no ChatGPT process exists; otherwise report and stop. The main classification weakness is not excessive authority, but over-attributing several runtime failures to “restart required.”

`accessibility_flag = false` is the strong launch-time signal that really does justify `NEEDS_HUMAN_RESTART`. By contrast, `composer not found`, `no main window`, or a mixed multi-process observation can also arise from transient startup, session state, non-chat UI, or instance ambiguity. Those states should be distinguished before prescribing restart.

No change to the fixed no-kill/no-restart safety constraint is recommended.
