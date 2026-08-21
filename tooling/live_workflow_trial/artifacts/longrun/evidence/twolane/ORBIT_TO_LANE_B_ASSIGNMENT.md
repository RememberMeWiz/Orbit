ORBIT-LANE-B-TOKEN

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: M0-LANE-B-001.

This is lane B of a two-lane concurrent supervision trial. Another lane is
running at the same time in a different conversation. Answer only about your own
work item, M0-LANE-B-001.

**Do not create or deliver any file. Answer in this conversation as plain chat.**

## Task

Orbit supervises several work items at once but can only display one ChatGPT conversation at a time, so lanes take turns using the window. A lane may also be placed on HOLD by PM while others continue. What is the right fairness and ordering policy for choosing which lane gets the window next, and what starvation or priority-inversion failure should Orbit explicitly guard against? Answer as a policy, not code.

Keep it to roughly 200-350 words. Substance over length.

## What to return

Write your answer in this conversation, exactly in this form:

    ORBIT_HANDOFF_BEGIN HANDOFF_M0-LANE-B-001_ARCHITECTURE_TO_ORBIT.md
    work_item: M0-LANE-B-001
    from: ARCHITECTURE
    to: ORBIT
    status: COMPLETE
    handoff_id: M0-LANE-B-001-0001
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
