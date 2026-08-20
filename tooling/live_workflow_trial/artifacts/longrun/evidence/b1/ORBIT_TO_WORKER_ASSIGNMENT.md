ORBIT-B1-LIVE-001-ASSIGNMENT

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-WF-B1-LIVE-001.

## Task

Review the failure-mode classification of Orbit's ChatGPT accessibility runtime
guard and report any runtime state it fails to classify correctly.

The guard exists because the bridge only works when the ChatGPT renderer
exposes a semantic accessibility tree, which in practice requires the app to
have been started with `--force-renderer-accessibility`. That is a launch-time
property: an app already running without the flag cannot be made to grow a
semantic tree.

### What the guard observes

A single read-only observation returns:

- `running` — any ChatGPT process exists
- `windowed` — at least one such process has a main window handle
- `trusted_path` — the executable path is inside the installed OpenAI.Codex package
- `accessibility_flag` — `--force-renderer-accessibility` appears on the process command line
- `accessibility_ready` — a ProseMirror composer was actually found in the UIA tree
- `descendants` — count of UIA descendants under the main window

### How it classifies those observations

| condition | status | reason_code |
| :--- | :--- | :--- |
| no process at all | UNAVAILABLE | `app-not-running` |
| running, path outside the package | NEEDS_HUMAN_RESTART | `app-untrusted-path` |
| running, trusted, no main window | NEEDS_HUMAN_RESTART | `app-has-no-window` |
| running, trusted, windowed, composer found | READY | `ok` |
| running, trusted, windowed, no composer, flag absent | NEEDS_HUMAN_RESTART | `accessibility-flag-absent` |
| running, trusted, windowed, no composer, flag present | NEEDS_HUMAN_RESTART | `accessibility-not-exposed` |
| the observation itself fails | UNAVAILABLE | the driver's own reason code |

### What it is allowed to do about them

Exactly two actions:

1. If nothing is running, start the app with the flag. The executable is
   resolved from the installed package rather than a pinned path.
2. Otherwise, report the status and the remedy, and stop.

It must **never** close, kill or restart a running app: the Product Owner may be
mid-conversation in that window, and an unattended process that ends a human's
session to unblock itself is a worse failure than staying blocked. The launch
operation refuses outright if any ChatGPT process already exists.

`accessibility-not-exposed` is re-observed a few times before being reported,
because a freshly shown window may still be coming up. The other running-but-
broken states are reported immediately.

## What to return

Return a single downloadable Markdown file named exactly:

    HANDOFF_M0-WF-B1-LIVE-001_WORKER_TO_ORBIT.md

It must begin with this header block, filled in:

```
# Accessibility Guard Review

## Header
- Work Item: `M0-WF-B1-LIVE-001`
- From: `WORKER`
- To: `ORBIT`
- Status: `COMPLETE`
- Handoff ID: `M0-WF-B1-LIVE-001-0001`
- Sequence: `1`
```

Then a `## Findings` section. For each finding give: the runtime state, what the
guard currently concludes, why that is wrong or incomplete, and what it should
conclude instead. If you find nothing wrong, say so explicitly and list the
states you checked.

Constraints on your answer:

- Judge only the classification and the two permitted actions. Do not propose
  giving the guard the ability to terminate or restart the app — that is a fixed
  safety constraint, not an oversight.
- Prefer concrete states a real Windows desktop can be in (multiple profiles,
  an app updating mid-session, a minimised or hidden window, a second monitor
  going away, a session lock, an app relaunched by the store updater) over
  abstract ones.
- Attach the file. Do not paste the contents as a chat message only.
