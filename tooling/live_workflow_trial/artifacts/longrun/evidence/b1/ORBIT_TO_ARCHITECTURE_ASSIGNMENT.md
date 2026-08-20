ORBIT-B1-LIVE-002-ARCH-REVIEW

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-WF-B1-LIVE-001.
PM directive: pmdir-20260820-1947-b1-live-002.

The attached file is the windows-worker's review of Orbit's ChatGPT
accessibility runtime guard, collected off the bridge and header-validated
(SHA-256 c028b97cd5a2062cea2415598c65f69dff7db150debbf9da2fdd79b998e3a6d1).

## Task

Review the changes Orbit made in response to that review, and say whether the
resulting classification is sound. Challenge anything that is wrong.

### What the guard now observes

One process is selected — the first with a main window, else the first at all —
and **every** field below is read from that same process: executable path,
command line, window handle, and the UIA tree under that window. Additionally:

- `windowed_count` / `instance_ambiguous` — how many processes own a window
- `session_locked` — `OpenInputDesktop` fails while the workstation is locked
  or a secure desktop is up
- `web_content_present` — a `Document` control exists in the tree
- `accessibility_ready` — a ProseMirror composer exists in the tree

### The new classification, in evaluation order

| # | condition | status | reason_code | remedy |
| :-- | :--- | :--- | :--- | :--- |
| 1 | observation failed | UNAVAILABLE | driver's own code | — |
| 2 | no process | UNAVAILABLE | `app-not-running` | Orbit starts it |
| 3 | session locked | UNAVAILABLE | `interactive-session-unavailable` | unlock Windows |
| 4 | path outside package | NEEDS_HUMAN_RESTART | `app-untrusted-path` | verify which process |
| 5 | >1 windowed instance | UNAVAILABLE | `multiple-instance-ambiguous` | close the extras |
| 6 | no window, no flag | NEEDS_HUMAN_RESTART | `accessibility-flag-absent` | relaunch with flag |
| 7 | no window, flag present | UNAVAILABLE | `window-not-ready` | wait / open window |
| 8 | composer found | READY | `ok` | — |
| 9 | no composer, no flag | NEEDS_HUMAN_RESTART | `accessibility-flag-absent` | relaunch with flag |
| 10 | no composer, flag, web content | UNAVAILABLE | `composer-not-present` | open a conversation |
| 11 | no composer, flag, no web content | NEEDS_HUMAN_RESTART | `accessibility-not-exposed` | relaunch with flag |

Rows 7, 10 and 11 are re-observed up to three times before being reported,
because a window under construction can resolve on its own. Rows 3, 4, 5, 6 and
9 are reported on the first observation: re-checking them only delays telling
the human what to do.

The two permitted actions are unchanged and non-negotiable: start the app when
no process exists, otherwise report and stop. Nothing in Orbit may close, kill
or restart a running app. `launch_app` refuses if any ChatGPT process exists,
and the driver contains no `Stop-Process`, `taskkill`, `Kill` or
`CloseMainWindow`.

## Questions to answer

1. Is the evaluation order right? In particular, is it correct to report the
   locked session (row 3) before the untrusted path (row 4), given a locked
   session means the tree could not be read at all?
2. Row 6 versus row 7: a process with no window and no flag is reported as
   `accessibility-flag-absent` rather than "no window". Is preferring the
   immutable cause over the observed-first cause the right rule in general?
3. Is `web_content_present` (a `Document` control) a sound discriminator
   between "renderer accessibility is dead" and "this view is not a chat"?
   If not, what would be sounder, given the guard may only observe?
4. Row 5 refuses `READY` whenever two instances own a window, even if a
   composer was found. Is refusing correct, or is it over-strict given the
   guard cannot close the extra window?
5. Any state in the table that is still classified with the wrong remedy, or
   any runtime state the table still does not cover.

## What to return

A single downloadable Markdown file named exactly:

    HANDOFF_M0-WF-B1-LIVE-001_ARCHITECTURE_TO_ORBIT.md

beginning with this header block, filled in:

```
# Accessibility Guard Classification Review

## Header
- Work Item: `M0-WF-B1-LIVE-001`
- From: `ARCHITECTURE`
- To: `ORBIT`
- Status: `COMPLETE`
- Handoff ID: `M0-WF-B1-LIVE-001-0002`
- Sequence: `2`
```

Then `## Verdict` (one of: sound as specified / sound with changes / not sound,
plus one sentence), then `## Findings` answering the five questions, then
`## Required Changes` if any, each stated as a concrete rule change.

Do not propose giving Orbit the ability to terminate or restart the app. That
is a fixed safety constraint, not an oversight. Attach the file rather than
pasting it as a chat message.
