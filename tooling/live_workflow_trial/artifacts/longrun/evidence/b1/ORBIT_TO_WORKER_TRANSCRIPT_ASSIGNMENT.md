ORBIT-B1-LIVE-005-TRANSCRIPT-COLLECT

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-WF-B1-LIVE-001.
PM directive: pmdir-20260821-0014-b1-live-005.

**Do not create, produce, or deliver any file. Do not use any file-artifact or
work mode. Answer entirely in this conversation as plain chat.** The whole point
of this task is that a handoff can come back without spending credits on a file.

## Context

Orbit just added a second way to collect a handoff. Previously a worker had to
return a downloadable file, which is what makes this app offer to escalate into
a paid work mode. Now a worker can write the handoff directly into the
conversation, delimited by two plain marker lines, and Orbit reads it out of the
transcript.

How Orbit reads it:

1. Focus the expected endpoint and verify the active-chat header matches.
2. Read the transcript tail.
3. Split it into turns using the app's own `ChatGPT said:` / `You said:`
   markers, and search **only assistant turns**. (Orbit's own assignment names
   the expected filename, so without this Orbit would find its own instructions.)
4. Within those turns, find every block starting `ORBIT_HANDOFF_BEGIN <name>`
   and ending `ORBIT_HANDOFF_END`, where `<name>` equals the expected filename
   exactly. A block with no end marker is skipped entirely.
5. Take the newest such block, write it to Orbit's inbox at the expected
   filename, then run the *same* validator a saved file goes through: filename
   must match `HANDOFF_<work>_<SENDER>_TO_<RECIPIENT>.md`, the work item in the
   filename must match, the `## Header` block must parse, the header work item
   must match, and the sender must match if one was expected.
6. If validation fails the file is deleted rather than left in the inbox.
7. Refuse if a file of that name is already collected. Refuse `.zip` on this
   path entirely.

Header values on this path must be **plain**, not wrapped in backticks: the
accessibility tree splits an inline-code value onto its own line, which arrives
as a malformed header.

## Task

Find holes in that. Specifically:

1. Can anything other than the intended worker get a handoff accepted? Consider
   quoted text inside an assistant turn, a worker echoing Orbit's assignment
   back, nested or overlapping BEGIN/END markers, a block inside a code fence,
   and the marker text appearing inside the handoff body itself.
2. Is "newest wins" right here, or can it be abused? Compare with the file path,
   where the app shows a discrete artifact card.
3. What can the accessibility tree do to a body between the worker writing it
   and Orbit reading it, and which of those corruptions would still pass
   validation rather than failing closed?
4. Is deleting an invalid body the right call, or does it destroy evidence
   somebody would want?
5. Anything the file path checks that this path does not.

Rank by whether a realistic worker or a realistic transcript could trigger it,
not by theoretical severity.

## What to return

Write your answer **in this conversation** as plain chat, exactly in this form:

    ORBIT_HANDOFF_BEGIN HANDOFF_M0-WF-B1-LIVE-001_WORKER_TO_ORBIT-2.md
    work_item: M0-WF-B1-LIVE-001
    from: WORKER
    to: ORBIT-2
    status: COMPLETE
    handoff_id: M0-WF-B1-LIVE-001-0004
    sequence: 4
    ORBIT_HANDOFF_BODY
    ...your findings, numbered, then a final "Highest-Priority Gap" paragraph...
    ORBIT_HANDOFF_END

Critical formatting rules, and the reason for each:

- The three marker lines and the six `key: value` lines must each be on their
  own line, at the start of the line, with **no** bullet, no numbering, no bold,
  no backticks and no code fence around them.
- Use exactly those six field names in lower case with underscores. Do not use
  Markdown headings or bullet lists for them.
- Everything after `ORBIT_HANDOFF_BODY` is free prose and may be formatted
  however you like.

The reason is measured, not stylistic: this text reaches Orbit through the
Windows accessibility tree, which keeps plain text and discards structure. A
Markdown heading arrives with its `#` stripped and a bullet list under it can
disappear entirely — that is exactly how the previous attempt at this task
failed. Flat `key: value` lines survive intact, so Orbit reads those and renders
the canonical Markdown handoff itself.
