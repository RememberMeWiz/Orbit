"""Orbit PM control envelope.

The PM chat is a conversation, and conversations contain prose. Prose is not
authority. Orbit executes a PM instruction only when it arrives inside an
explicit delimited envelope that names the exact pending request it answers.

The protections that matter:

* a directive must quote the `request_id` of the *currently pending* request, so
  a stale message scrolled up in the chat cannot be replayed;
* a directive must name the work item it belongs to, so a decision about one
  item cannot move another;
* each `directive_id` is consumed once and thereafter inert;
* an unparseable or absent envelope leaves Orbit waiting and reporting why --
  never guessing to keep the pipeline moving.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from workflow.core.storage import atomic_write_json, utc_now_iso

ENVELOPE_VERSION = "0.1"
ENVELOPE_MARKER = "ORBIT_DIRECTIVE"

# Actions PM may direct. Anything outside this set is refused even inside a
# well-formed envelope.
DIRECTIVE_ACTIONS: Tuple[str, ...] = (
    "DISPATCH_TO_ROLE",
    "COLLECT_RESULT",
    "HOLD",
    "STOP",
    "ABANDON_REQUEST",
)

_BLOCK_RE = re.compile(
    r"```(?:\w+)?\s*\n?\s*" + ENVELOPE_MARKER + r"\b(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_FIELD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")
# A value still wearing its <angle brackets> was never filled in.
_PLACEHOLDER_RE = re.compile(r"^<.*>?$|^<")


class DirectiveError(ValueError):
    """Raised only for programmer error. Rejections are returned, not raised."""


@dataclass(frozen=True)
class PMRequest:
    """What Orbit asks PM when it will not proceed on its own."""

    request_id: str
    work_item: str
    reason: str
    current_owner: str = ""
    workflow_state: Dict[str, Any] = field(default_factory=dict)
    artifact_id: str = ""
    artifact_digest: str = ""
    safe_actions: Tuple[str, ...] = ()

    def render(self) -> str:
        """Human-readable and machine-readable in one message."""
        lines = [
            "ORBIT_PM_REQUEST",
            f"version: {ENVELOPE_VERSION}",
            f"request_id: {self.request_id}",
            f"work_item: {self.work_item}",
            f"current_owner: {self.current_owner}",
            f"reason: {self.reason}",
        ]
        if self.artifact_id:
            lines.append(f"artifact_id: {self.artifact_id}")
        if self.artifact_digest:
            lines.append(f"artifact_sha256: {self.artifact_digest}")
        if self.safe_actions:
            lines.append("safe_actions: " + ", ".join(self.safe_actions))
        if self.workflow_state:
            lines.append("workflow_state: " + json.dumps(self.workflow_state, sort_keys=True))
        lines.append("awaiting: ORBIT_DIRECTIVE")
        # Fenced so it is unmistakably machine-generated in the chat, and so PM
        # can see exactly which request_id a directive has to answer.
        return "```\n" + "\n".join(lines) + "\n```"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "work_item": self.work_item,
            "reason": self.reason,
            "current_owner": self.current_owner,
            "workflow_state": dict(self.workflow_state),
            "artifact_id": self.artifact_id,
            "artifact_digest": self.artifact_digest,
            "safe_actions": list(self.safe_actions),
        }


@dataclass(frozen=True)
class PMDirective:
    directive_id: str
    request_id: str
    work_item: str
    action: str
    target_endpoint: str = ""
    artifact_id: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "directive_id": self.directive_id,
            "request_id": self.request_id,
            "work_item": self.work_item,
            "action": self.action,
            "target_endpoint": self.target_endpoint,
            "artifact_id": self.artifact_id,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "PMDirective":
        """Rebuild a directive that was journalled or passed between steps.

        Unknown keys are dropped rather than accepted: a directive is only ever
        the fields the parser recognises, so a round trip through JSON cannot
        smuggle an extra one back in.
        """
        return cls(
            directive_id=str(payload.get("directive_id", "")),
            request_id=str(payload.get("request_id", "")),
            work_item=str(payload.get("work_item", "")),
            action=str(payload.get("action", "")),
            target_endpoint=str(payload.get("target_endpoint", "")),
            artifact_id=str(payload.get("artifact_id", "")),
            notes=str(payload.get("notes", "")),
        )


@dataclass(frozen=True)
class DirectiveVerdict:
    accepted: bool
    reason_code: str
    directive: Optional[PMDirective] = None
    detail: str = ""


# How the app labels each turn in the transcript. These are the only provenance
# signal the accessibility tree offers, and they are what separates "PM decided
# this" from "someone put this text in the conversation".
ASSISTANT_MARKER = re.compile(r"(?m)^\s*ChatGPT said:\s*$")
USER_MARKER = re.compile(r"(?m)^\s*You said:\s*$")


def assistant_turns(text: str) -> List[str]:
    """The stretches of transcript the assistant itself authored.

    Everything else -- what Orbit posted, what a human pasted, quoted logs,
    attachments rendered inline -- is somebody else's text sitting in the same
    conversation, and must not be able to authorise anything.

    A transcript with no turn markers yields nothing rather than the whole
    blob. Absent provenance is not weak provenance; it is none, and the caller
    fails closed on an empty list.
    """
    turns: List[str] = []
    for match in ASSISTANT_MARKER.finditer(text):
        rest = text[match.end():]
        nxt = USER_MARKER.search(rest)
        turns.append(rest[:nxt.start()] if nxt else rest)
    return turns


def _candidate_bodies(text: str) -> List[str]:
    """Every stretch of text that could be an envelope, oldest first.

    A transcript is an append-only log of a whole conversation, so the marker
    appears many times: in prose, in the reply template Orbit itself posted, and
    finally in PM's actual answer. All of them are collected here; choosing
    between them is the caller's job.
    """
    bodies = [m.group("body") for m in _BLOCK_RE.finditer(text)]
    bodies.extend(text[m.end():] for m in
                  re.finditer(r"(?m)^\s*" + ENVELOPE_MARKER + r"\s*$", text))
    return bodies


def _parse_one(body: str) -> Tuple[Optional[PMDirective], str]:
    fields: Dict[str, str] = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        m = _FIELD_RE.match(line)
        if not m:
            if fields:
                break  # end of the field block
            continue
        fields[m.group(1).strip().lower()] = m.group(2).strip()

    version = fields.get("version", "")
    if version and version != ENVELOPE_VERSION:
        return None, f"directive-version-unsupported:{version}"

    # Orbit's own request carries a reply template, so the transcript always
    # contains an envelope shaped exactly like a directive but filled with
    # <angle-bracketed> placeholders. Reading that back as a decision would be
    # Orbit taking instruction from itself, so placeholders are refused outright
    # -- which also catches a PM who pasted the template without editing it.
    unfilled = sorted(k for k, v in fields.items() if _PLACEHOLDER_RE.match(v))
    if unfilled:
        return None, "directive-template-not-filled-in:" + ",".join(unfilled)

    required = ("request_id", "directive_id", "work_item", "action")
    missing = [k for k in required if not fields.get(k)]
    if missing:
        return None, "directive-missing:" + ",".join(missing)

    action = fields["action"].upper()
    if action not in DIRECTIVE_ACTIONS:
        return None, f"directive-action-not-allowlisted:{action}"

    return PMDirective(
        directive_id=fields["directive_id"],
        request_id=fields["request_id"],
        work_item=fields["work_item"],
        action=action,
        target_endpoint=fields.get("target_endpoint", ""),
        artifact_id=fields.get("artifact_id", ""),
        notes=fields.get("notes", ""),
    ), "directive-parsed"


def parse_envelope(text: str, *, require_assistant_turn: bool = True
                   ) -> Tuple[Optional[PMDirective], str]:
    """Extract PM's most recent decision, or say why there isn't one.

    Two independent restrictions, and both matter.

    *Provenance.* Only text the assistant authored is searched. A well-formed
    envelope is trivial to write, so field-level validity says nothing about
    who wrote it: Orbit's own reply template, a pasted log, an attachment
    rendered inline, or a message crafted to name the live request_id would all
    pass every field check. Scoping to assistant turns is what makes the
    envelope a decision rather than a string that appeared in a conversation.

    *Recency.* Within those turns, newest-first, because a transcript
    accumulates and PM's latest decision is the operative one. A malformed
    candidate is skipped rather than fatal; only when nothing parses is a
    reason returned, and it is the newest candidate's.

    `require_assistant_turn=False` exists for parsing a body already known to
    have come from a trusted channel. It must never be set from transcript text.
    """
    if not text:
        return None, "directive-absent"

    if require_assistant_turn:
        turns = assistant_turns(text)
        if not turns:
            return None, "directive-provenance-unknown"
        bodies = [b for turn in turns for b in _candidate_bodies(turn)]
    else:
        bodies = _candidate_bodies(text)
    if not bodies:
        return None, "directive-absent"

    newest_reason = "directive-absent"
    for index, body in enumerate(reversed(bodies)):
        directive, reason = _parse_one(body)
        if directive is not None:
            return directive, reason
        if index == 0:
            newest_reason = reason
    return None, newest_reason


class PMBridgeState:
    """Durable pending-request and consumed-directive ledger."""

    def __init__(self, path: Path, *, work_item: str):
        self.path = Path(path)
        self.work_item = work_item

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            state = {
                "schema_version": "orbit.pm-bridge/0.1-draft",
                "work_item": self.work_item,
                "pending_request": None,
                "consumed_directive_ids": [],
                "state_revision": 0,
                "updated_at": utc_now_iso(),
            }
            self.save(state)
            return state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state.setdefault("consumed_directive_ids", [])
        return state

    def save(self, state: Dict[str, Any]) -> None:
        state["state_revision"] = int(state.get("state_revision", 0)) + 1
        state["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, state)

    def open_request(self, request: PMRequest) -> Dict[str, Any]:
        state = self.load()
        state["pending_request"] = request.to_dict()
        self.save(state)
        return state

    def evaluate(self, text: str) -> DirectiveVerdict:
        """Decide whether PM chat text authorises anything right now."""
        state = self.load()
        pending = state.get("pending_request")
        if not pending:
            return DirectiveVerdict(False, "no-pending-request")

        directive, reason = parse_envelope(text)
        if directive is None:
            # Prose without a valid envelope is not authority. Keep waiting.
            return DirectiveVerdict(False, reason)

        if directive.request_id != pending["request_id"]:
            return DirectiveVerdict(False, "directive-stale-request-id", directive,
                                    f"pending {pending['request_id']}, got {directive.request_id}")
        if directive.work_item != pending["work_item"]:
            return DirectiveVerdict(False, "directive-work-item-mismatch", directive,
                                    f"pending {pending['work_item']}, got {directive.work_item}")
        if directive.directive_id in state["consumed_directive_ids"]:
            return DirectiveVerdict(False, "directive-already-consumed", directive)

        return DirectiveVerdict(True, "directive-accepted", directive)

    def consume(self, directive: PMDirective) -> Dict[str, Any]:
        state = self.load()
        if directive.directive_id not in state["consumed_directive_ids"]:
            state["consumed_directive_ids"].append(directive.directive_id)
        state["pending_request"] = None
        self.save(state)
        return state


def request_identity(work_item: str, reason: str, nonce: str) -> str:
    payload = f"{work_item}|{reason}|{nonce}".encode("utf-8")
    return "pmreq-" + hashlib.sha256(payload).hexdigest()[:20]
