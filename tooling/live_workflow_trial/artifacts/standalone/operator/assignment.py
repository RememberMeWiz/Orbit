"""Render the assignment Orbit sends a worker, including how to answer.

An assignment that states the task but not the reply format is not an
assignment, it is a hope. The supervisor previously sent

    Assignment for W-1: <objective>

which tells a worker nothing about the markers, the filename, or the field
shape, so the answer could not be collected and the lane blocked much later
looking like the worker's fault.

Everything here was learned by watching real replies come back through the
Windows accessibility tree:

* flat ``key: value`` lines survive; Markdown does not. A heading arrives with
  its ``#`` stripped and the bullet list under it can vanish entirely, so the
  worker sends fields and Orbit renders the canonical Markdown itself;
* asking for a *downloadable file* makes the app offer a paid work mode, so a
  text handoff asks for text;
* the markers must appear exactly once, because collection requires exactly one
  eligible block and an assistant that quotes the format back creates a second.
"""
from __future__ import annotations

from typing import Optional

# Recipient in the handoff filename. Fixed: the reply comes back to Orbit.
RECIPIENT = "ORBIT"


def handoff_filename(work_item: str, sender_role: str) -> str:
    """The name both sides must agree on, in the canonical handoff shape."""
    return f"HANDOFF_{work_item}_{sender_role}_TO_{RECIPIENT}.md"


def sender_role_for(endpoint_id: str, registry=None) -> str:
    """The role token a given endpoint signs its handoffs with.

    Taken from the committed endpoint registry so it cannot drift from the
    routing table. Falls back to a slug derived from the endpoint id, which
    still satisfies the filename grammar.
    """
    if registry is not None:
        endpoint = registry.get(endpoint_id)
        if endpoint is not None and getattr(endpoint, "role_id", ""):
            return str(endpoint.role_id).upper()
    return "".join(ch for ch in str(endpoint_id).upper() if ch.isalnum() or ch == "-") or "WORKER"


def render(work_item: str, objective: str, *, sender_role: str,
           token: str, sequence: int = 1, handoff_id: Optional[str] = None) -> str:
    """The full assignment text, task and return contract together."""
    filename = handoff_filename(work_item, sender_role)
    return f"""{token}

Posted by the Orbit local program through the ChatGPT desktop accessibility
bridge. No file was carried by the Product Owner. Work item: {work_item}.

**Do not create or deliver any file. Answer in this conversation as plain chat.**
Asking for a downloadable artifact makes this app offer a paid work mode, and
everything below can be carried as text.

## Task

{objective}

## What to return

Write your answer in this conversation, exactly in this form:

    ORBIT_HANDOFF_BEGIN {filename}
    work_item: {work_item}
    from: {sender_role}
    to: {RECIPIENT}
    status: COMPLETE
    handoff_id: {handoff_id or f"{work_item}-{sequence:04d}"}
    sequence: {sequence}
    ORBIT_HANDOFF_BODY
    ...your answer...
    ORBIT_HANDOFF_END

Formatting rules, and the reason for each:

- The three marker lines and the six `key: value` lines must each be on their
  own line, at the start of the line, with no bullet, no numbering, no bold, no
  backticks and no code fence around them.
- Use exactly those six field names, lower case with underscores.
- Everything after ORBIT_HANDOFF_BODY is free prose, formatted however you like.
- Write the marker lines exactly once each. Do not quote, echo or demonstrate
  them anywhere else in your message: Orbit requires exactly one eligible block
  and refuses ambiguity, so a second copy makes the whole answer uncollectable.

The reason is measured, not stylistic. This text reaches Orbit through the
Windows accessibility tree, which keeps plain text and discards structure: a
Markdown heading arrives with its `#` stripped and a bullet list under it can
disappear entirely. Flat `key: value` lines survive intact, so Orbit reads those
and renders the canonical Markdown handoff itself.

If you cannot complete the task, still return the block with
`status: BLOCKED` and explain why in the body. A refusal Orbit can read is worth
more than a perfect answer it cannot.
"""
