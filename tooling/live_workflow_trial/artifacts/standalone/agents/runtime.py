"""Orbit-managed local agent roles.

An agent task is a governed record, not a process. It carries immutable identity,
a role, an objective, the capabilities it was granted, and a status that is only
ever advanced by deterministic code in this module.

COMPLETE is deliberately not reachable from a brain result. A model saying it
finished, a subprocess exiting, or an absence of output are all insufficient:
only ``mark_complete`` -- called by governed code after the workflow engine has
actually accepted a handoff -- may set it.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from workflow.core.storage import atomic_write_json, utc_now_iso

from ..brain.contracts import LocalBrainRequest, LocalBrainResult

AGENT_STATUSES: Tuple[str, ...] = (
    "ASSIGNED",
    "WORKING",
    "READY_FOR_REVIEW",
    "BLOCKED",
    "NEEDS_DECISION",
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
    "COMPLETE",
)

TERMINAL_AGENT_STATUSES: Tuple[str, ...] = (
    "READY_FOR_REVIEW",
    "BLOCKED",
    "NEEDS_DECISION",
    "FAILED_FINAL",
    "COMPLETE",
)

# Brain outcome -> agent status. "OK" reaches READY_FOR_REVIEW, never COMPLETE.
_BRAIN_TO_AGENT = {
    "OK": "READY_FOR_REVIEW",
    "BLOCKED": "BLOCKED",
    "NEEDS_DECISION": "NEEDS_DECISION",
    "FAILED_RETRYABLE": "FAILED_RETRYABLE",
    "FAILED_FINAL": "FAILED_FINAL",
}


class AgentRuntimeError(ValueError):
    """Raised when an agent task violates identity or authority rules."""


def task_identity(work_item: str, role: str, objective: str) -> str:
    payload = "|".join([work_item, role, objective]).encode("utf-8")
    return "task-" + hashlib.sha256(payload).hexdigest()[:24]


@dataclass
class AgentTask:
    work_item: str
    role: str
    objective: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    allowed_capabilities: Tuple[str, ...] = ()
    status: str = "ASSIGNED"
    result: Optional[Dict[str, Any]] = None
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        for name in ("work_item", "role", "objective"):
            if not str(getattr(self, name) or "").strip():
                raise AgentRuntimeError(f"agent-task-missing:{name}")
        if self.status not in AGENT_STATUSES:
            raise AgentRuntimeError(f"agent-status-not-allowlisted:{self.status}")
        if not self.created_at:
            self.created_at = utc_now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    @property
    def task_id(self) -> str:
        return task_identity(self.work_item, self.role, self.objective)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_AGENT_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "work_item": self.work_item,
            "role": self.role,
            "objective": self.objective,
            "inputs": dict(self.inputs),
            "allowed_capabilities": list(self.allowed_capabilities),
            "status": self.status,
            "result": copy.deepcopy(self.result),
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(value: Dict[str, Any]) -> "AgentTask":
        return AgentTask(
            work_item=value["work_item"],
            role=value["role"],
            objective=value["objective"],
            inputs=dict(value.get("inputs", {})),
            allowed_capabilities=tuple(value.get("allowed_capabilities", ())),
            status=value.get("status", "ASSIGNED"),
            result=value.get("result"),
            attempts=int(value.get("attempts", 0)),
            created_at=value.get("created_at", ""),
            updated_at=value.get("updated_at", ""),
        )


class AgentTaskStore:
    """Durable task ledger. Restart reads it instead of re-running work."""

    def __init__(self, path: Path, *, work_item: str):
        self.path = path
        self.work_item = work_item

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            state = {
                "schema_version": "orbit.agent-tasks/0.1-draft",
                "work_item": self.work_item,
                "tasks": {},
                "state_revision": 0,
                "updated_at": utc_now_iso(),
            }
            self.save(state)
            return state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        existing = state.get("work_item")
        if existing is not None and existing != self.work_item:
            # Opening another work item's task ledger would let a result be
            # attributed to the wrong item.
            raise AgentRuntimeError("agent-store-work-item-mismatch")
        state.setdefault("tasks", {})
        return state

    def save(self, state: Dict[str, Any]) -> None:
        state["state_revision"] = int(state.get("state_revision", 0)) + 1
        state["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, state)

    def get(self, task_id: str) -> Optional[AgentTask]:
        record = self.load()["tasks"].get(task_id)
        return AgentTask.from_dict(record) if record else None

    def put(self, task: AgentTask) -> AgentTask:
        state = self.load()
        task.updated_at = utc_now_iso()
        state["tasks"][task.task_id] = task.to_dict()
        self.save(state)
        return task

    def all_tasks(self):
        return [AgentTask.from_dict(v) for v in self.load()["tasks"].values()]


class LocalAgentRuntime:
    """Runs bounded role tasks against the local brain interface."""

    def __init__(self, store: AgentTaskStore, router, *, stop_path: Optional[Path] = None):
        self.store = store
        self.router = router
        self.stop_path = stop_path

    def is_stopped(self) -> bool:
        return bool(self.stop_path and self.stop_path.is_file())

    def ensure_task(self, task: AgentTask) -> AgentTask:
        """Register a task idempotently.

        Identity is derived from (work_item, role, objective), so re-registering
        the same logical task after a restart returns the existing record with
        its status intact rather than resetting it to ASSIGNED.
        """
        if task.work_item != self.store.work_item:
            raise AgentRuntimeError("agent-task-work-item-mismatch")
        existing = self.store.get(task.task_id)
        if existing is not None:
            return existing
        return self.store.put(task)

    def run(self, task: AgentTask) -> AgentTask:
        task = self.ensure_task(task)

        if task.is_terminal:
            # Already resolved. Re-running would risk a second advancement.
            return task

        if self.is_stopped():
            return task  # stays ASSIGNED; STOP never starts new agent work

        task.status = "WORKING"
        task.attempts += 1
        self.store.put(task)

        request = LocalBrainRequest(
            task_id=task.task_id,
            role=task.role,
            objective=task.objective,
            context=dict(task.inputs),
            allowed_capabilities=tuple(task.allowed_capabilities),
            result_schema=task.inputs.get("result_schema", {}) or {},
        )
        brain_result: LocalBrainResult = self.router.reason(request)

        if brain_result.task_id != task.task_id:
            task.status = "FAILED_FINAL"
            task.result = {
                "reason_code": "agent-result-task-mismatch",
                "detail": f"expected {task.task_id}, got {brain_result.task_id}",
            }
            return self.store.put(task)

        task.status = _BRAIN_TO_AGENT.get(brain_result.status, "FAILED_FINAL")
        task.result = brain_result.to_dict()
        return self.store.put(task)

    def mark_complete(self, task: AgentTask, *, evidence: Dict[str, Any]) -> AgentTask:
        """Only governed code may call this, after the workflow engine accepted.

        This is the single path to COMPLETE. It is intentionally unreachable
        from any brain or agent output.
        """
        if task.status != "READY_FOR_REVIEW":
            raise AgentRuntimeError(f"agent-complete-requires-ready-for-review:{task.status}")
        task.status = "COMPLETE"
        task.result = dict(task.result or {})
        task.result["completion_evidence"] = evidence
        return self.store.put(task)
