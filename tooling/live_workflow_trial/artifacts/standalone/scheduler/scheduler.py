"""Local scheduler: drives the accepted workflow with local agents only.

This is the piece that removes the human from routine internal handoffs. It reads
persisted WorkflowState, creates a local task for whichever role currently owns
the work item, runs it through the local brain, and -- when the role produces a
result -- writes a normal handoff artifact into the workflow inbox so the
existing engine validates and routes it exactly as it would a human-delivered
one.

It deliberately does not shortcut the engine. Transitions, replay protection,
digests, routing and approval gates all remain the engine's decisions; the
scheduler only supplies work and carries results to the inbox.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from workflow.core.storage import atomic_write_json, utc_now_iso

from ..agents.runtime import AgentTask, LocalAgentRuntime

DEFAULT_OBJECTIVES = {
    "WORKER": "Produce the deliverable for {work_item}",
    "TL": "Review the WORKER deliverable for {work_item}",
    "QA": "Adversarially verify the reviewed deliverable for {work_item}",
    "PM": "Record the product disposition for {work_item}",
}

# Agent outcome -> handoff envelope Status. The engine's own vocabulary.
_AGENT_TO_HANDOFF_STATUS = {
    "READY_FOR_REVIEW": "COMPLETE",
    "BLOCKED": "BLOCKED",
    "NEEDS_DECISION": "NEEDS_DECISION",
}

HANDOFF_TEMPLATE = """# Orbit Handoff

## Header
- Work Item: {work_item}
- From: {sender}
- To: {recipient}
- Status: {status}
- Handoff ID: {handoff_id}
- Sequence: {sequence}

## Executive Summary
{summary}

## Provenance
- Produced by: Orbit local agent runtime
- Brain provider: {provider}
- Task: {task_id}
"""


class SchedulerLedger:
    """Records which agent tasks already emitted a handoff.

    Without this a restart between "write handoff" and "engine accepted" could
    emit a second artifact for the same task. The engine would reject it as a
    replay, but the ledger keeps the workspace clean and the intent explicit.
    """

    def __init__(self, path: Path, *, work_item: str):
        self.path = path
        self.work_item = work_item

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            state = {
                "schema_version": "orbit.scheduler-state/0.1-draft",
                "work_item": self.work_item,
                "emitted": {},
                "state_revision": 0,
                "updated_at": utc_now_iso(),
            }
            self.save(state)
            return state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state.setdefault("emitted", {})
        return state

    def save(self, state: Dict[str, Any]) -> None:
        state["state_revision"] = int(state.get("state_revision", 0)) + 1
        state["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, state)

    def emitted(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self.load()["emitted"].get(task_id)

    def record(self, task_id: str, payload: Dict[str, Any]) -> None:
        state = self.load()
        state["emitted"][task_id] = payload
        self.save(state)


class LocalScheduler:
    def __init__(
        self,
        *,
        root: Path,
        manifest: Dict[str, Any],
        engine,
        reconciler,
        runtime: LocalAgentRuntime,
        ledger: SchedulerLedger,
        objectives: Optional[Dict[str, str]] = None,
        capabilities: Optional[Dict[str, tuple]] = None,
        settle: Optional[float] = None,
    ):
        self.root = root
        self.manifest = manifest
        self.engine = engine
        self.reconciler = reconciler
        self.runtime = runtime
        self.ledger = ledger
        self.objectives = dict(objectives or DEFAULT_OBJECTIVES)
        self.capabilities = dict(capabilities or {})
        self.paths = reconciler.runtime_paths
        # The reconciler only accepts an artifact that has been size/mtime stable
        # for the configured window, so a tick must outlast it.
        self.settle = settle if settle is not None else float(manifest.get("stable_window_seconds", 0.25))

    # -- helpers ---------------------------------------------------------

    def is_stopped(self) -> bool:
        return self.reconciler.is_stopped()

    def _objective(self, role: str) -> str:
        template = self.objectives.get(role, "Advance {work_item} as " + role)
        return template.format(work_item=self.manifest["work_item"], role=role)

    def _handoff_id(self, task_id: str, sequence: int) -> str:
        payload = f"{self.manifest['work_item']}|{task_id}|{sequence}".encode("utf-8")
        return "local-" + hashlib.sha256(payload).hexdigest()[:20]

    def _write_handoff(self, *, sender: str, recipient: str, status: str, handoff_id: str, sequence: int, summary: str, provider: str, task_id: str) -> Path:
        name = f"HANDOFF_{self.manifest['work_item']}_{sender}_TO_{recipient}.md"
        final = self.paths.inbox / name
        body = HANDOFF_TEMPLATE.format(
            work_item=self.manifest["work_item"],
            sender=sender,
            recipient=recipient,
            status=status,
            handoff_id=handoff_id,
            sequence=sequence,
            summary=summary,
            provider=provider,
            task_id=task_id,
        )
        # Write via a temp name then replace, so the reconciler never observes a
        # partially written artifact.
        self.paths.inbox.mkdir(parents=True, exist_ok=True)
        temp = final.with_suffix(".partial")
        temp.write_text(body, encoding="utf-8")
        temp.replace(final)
        return final

    def _reconcile(self) -> list:
        """Let the accepted reconciler observe and route what we just wrote."""
        results = self.reconciler.scan_once()
        if not results:
            time.sleep(self.settle + 0.05)
            results = self.reconciler.scan_once()
        return results

    # -- main loop -------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        """Advance the work item by at most one role step."""
        if self.is_stopped():
            return {"action": "STOPPED", "reason_code": "stop-active"}

        state = self.engine.store.load()
        owner = state["current_owner_role"]
        work_state = state.get("work_state")

        if work_state == "COMPLETE":
            return {"action": "COMPLETE", "owner": owner}

        # A product decision or blocker is surfaced, never guessed at.
        if work_state in ("NEEDS_DECISION", "BLOCKED"):
            return {"action": "AWAITING_HUMAN", "reason_code": work_state.lower(), "owner": owner, "blocker": state.get("blocker_state")}
        if state.get("delivery_state") == "APPROVAL_PENDING":
            return {"action": "AWAITING_APPROVAL", "reason_code": "approval-required", "owner": owner, "pending": state.get("pending_approval")}

        recipient = self.manifest["valid_transitions"].get(owner)
        if recipient is None:
            return {"action": "TERMINAL_ROLE", "owner": owner}

        task = AgentTask(
            work_item=self.manifest["work_item"],
            role=owner,
            objective=self._objective(owner),
            inputs={"work_state": work_state, "recipient": recipient},
            allowed_capabilities=tuple(self.capabilities.get(owner, ())),
        )
        task = self.runtime.run(task)

        if task.status in ("FAILED_RETRYABLE", "FAILED_FINAL"):
            return {"action": "AGENT_FAILED", "status": task.status, "owner": owner, "task_id": task.task_id, "result": task.result}

        handoff_status = _AGENT_TO_HANDOFF_STATUS.get(task.status)
        if handoff_status is None:
            return {"action": "AGENT_NOT_READY", "status": task.status, "owner": owner, "task_id": task.task_id}

        already = self.ledger.emitted(task.task_id)
        if already:
            # This task already produced its artifact. Reconcile whatever is
            # pending rather than emitting a duplicate.
            results = self._reconcile()
            return {"action": "ALREADY_EMITTED", "task_id": task.task_id, "handoff_id": already["handoff_id"], "results": results}

        sequence = int(state.get("last_sequence", 0)) + 1
        handoff_id = self._handoff_id(task.task_id, sequence)
        result_payload = task.result or {}
        summary = str((result_payload.get("result") or {}).get("summary") or result_payload.get("detail") or self._objective(owner))
        provider = str(result_payload.get("provider", "unknown"))

        path = self._write_handoff(
            sender=owner,
            recipient=recipient,
            status=handoff_status,
            handoff_id=handoff_id,
            sequence=sequence,
            summary=summary,
            provider=provider,
            task_id=task.task_id,
        )
        self.ledger.record(task.task_id, {"handoff_id": handoff_id, "sequence": sequence, "path": str(path), "emitted_at": utc_now_iso()})

        results = self._reconcile()
        accepted = [r for r in results if r.get("validation_result") == "accepted"]
        if accepted and task.status == "READY_FOR_REVIEW":
            # The engine accepted the artifact. Only now may the task complete.
            self.runtime.mark_complete(task, evidence={"handoff_id": handoff_id, "receipt_ids": [r.get("receipt_id") for r in accepted]})

        return {
            "action": "ADVANCED" if accepted else "EMITTED",
            "owner": owner,
            "recipient": recipient,
            "task_id": task.task_id,
            "handoff_id": handoff_id,
            "sequence": sequence,
            "handoff_status": handoff_status,
            "provider": provider,
            "results": results,
        }

    def run(self, max_ticks: int = 10) -> list:
        """Tick until the work item settles, a human is needed, or budget runs out."""
        transcript = []
        for _ in range(max_ticks):
            outcome = self.tick()
            transcript.append(outcome)
            if outcome["action"] in ("COMPLETE", "STOPPED", "AWAITING_HUMAN", "AWAITING_APPROVAL", "TERMINAL_ROLE", "AGENT_FAILED", "AGENT_NOT_READY"):
                break
        return transcript
