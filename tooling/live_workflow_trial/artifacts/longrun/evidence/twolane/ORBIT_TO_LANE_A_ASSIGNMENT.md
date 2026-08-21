ORBIT-LANE-A-TOKEN

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-LANE-A-001.

This is lane A of a two-lane concurrent supervision trial. Another lane is
running at the same time in a different conversation. Answer only about your own
work item, M0-LANE-A-001.

**Do not create or deliver any file. Answer in this conversation as plain chat.**

## Task

Orbit now supervises several work items at once, multiplexing one visible ChatGPT conversation. Each lane has its own durable state directory and its own PM request id. What is the most likely way two concurrent lanes could still interfere with each other on a single desktop, given that only one conversation can be visible at a time? Name the single highest-risk interference you can think of and what Orbit should check to detect it.

Keep it to roughly 200-350 words. Substance over length.

## What to return

Write your answer in this conversation, exactly in this form:

    ORBIT_HANDOFF_BEGIN HANDOFF_M0-LANE-A-001_WORKER_TO_ORBIT.md
    work_item: M0-LANE-A-001
    from: WORKER
    to: ORBIT
    status: COMPLETE
    handoff_id: M0-LANE-A-001-0001
    sequence: 1
    ORBIT_HANDOFF_BODY
    ...your answer...
    ORBIT_HANDOFF_END

Formatting rules, and the reason for each:

- The three marker lines and the six `key: value` lines must each be on their
  own line, at the start of the line, with no bullet, no numbering, no bold, no
  backticks and no code fence around them.
- Use exactly those six field names, lower case with underscores.
- Everything after ORBIT_HANDOFF_BODY is free prose.
- Write the marker lines exactly once each. Do not quote or demonstrate them
  anywhere else: Orbit requires exactly one eligible block and refuses ambiguity.

This text reaches Orbit through the Windows accessibility tree, which keeps
plain text and discards structure, so a Markdown heading arrives with its # 
stripped and a bullet list under it can vanish. Flat key: value lines survive.
