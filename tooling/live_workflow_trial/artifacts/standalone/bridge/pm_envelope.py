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
from typing import Any, Dict, Optional, Tuple

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


@dataclass(frozen=True)
class DirectiveVerdict:
    accepted: bool
    reason_code: str
    directive: Optional[PMDirective] = None
    detail: str = ""


def parse_envelope(text: str) -> Tuple[Optional[PMDirective], str]:
    """Extract a directive from PM chat text, or say why there isn't one."""
    if not text:
        return None, "directive-absent"
    match = _BLOCK_RE.search(text)
    if match:
        body = match.group("body")
    else:
        # Accept a bare block too, but only when the marker starts a line: prose
        # that merely mentions the word must not be mistaken for an envelope.
        idx = re.search(r"(?m)^\s*" + ENVELOPE_MARKER + r"\s*$", text)
        if not idx:
            return None, "directive-absent"
        body = text[idx.end():]

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
