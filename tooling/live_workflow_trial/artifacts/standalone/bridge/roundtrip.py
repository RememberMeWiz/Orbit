"""One full zero-courier cycle, run as a single governed sequence.

    Orbit PM  ->  Orbit  ->  Worker  ->  Orbit  ->  Orbit PM

The Product Owner's only involvement is the decision itself, expressed as an
ORBIT_DIRECTIVE in the PM conversation. Everything after that -- carrying the
assignment to the worker, waiting for it, collecting the result, validating it,
and reporting back -- is Orbit's job. The measure of success is therefore not
"did it finish" but *courier actions = 0*: no human copied, pasted, saved, or
re-attached anything.

Every step is journalled before and after it happens, so an interrupted cycle
can be read afterwards to see exactly how far it got, and each step is
individually resumable through the same durable state the CLI verbs use.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from workflow.core.storage import utc_now_iso

from .accessibility import AccessibilityGuard
from .orchestrator import ApprenticeLoop, LoopOutcome
from .pm_envelope import PMDirective

# Steps in order. Named so the journal reads as a narrative rather than a log.
STEPS = ("preflight", "wake_pm", "await_directive", "dispatch",
         "await_worker", "collect", "report_to_pm")


@dataclass
class CycleResult:
    completed: bool = False
    stopped_at: str = ""
    reason_code: str = ""
    courier_actions: int = 0
    steps: List[Dict[str, Any]] = field(default_factory=list)
    directive: Optional[Dict[str, Any]] = None
    artifact: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "completed": self.completed,
            "stopped_at": self.stopped_at,
            "reason_code": self.reason_code,
            "courier_actions": self.courier_actions,
            "directive": self.directive,
            "artifact": self.artifact,
            "steps": list(self.steps),
        }


class RoundTrip:
    """Drives one cycle. Holds no state of its own beyond the journal path."""

    def __init__(
        self,
        loop: ApprenticeLoop,
        *,
        journal_path: Optional[Path] = None,
        guard: Optional[AccessibilityGuard] = None,
        allow_launch: bool = True,
        observer: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.loop = loop
        self.journal_path = journal_path
        self.guard = guard if guard is not None else AccessibilityGuard(loop.adapter.driver)
        self.allow_launch = allow_launch
        self.observer = observer

    # -- journal ---------------------------------------------------------

    def _note(self, step: str, outcome: Dict[str, Any]) -> Dict[str, Any]:
        entry = {"at": utc_now_iso(), "work_item": self.loop.work_item,
                 "step": step, **outcome}
        if self.journal_path is not None:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        if self.observer is not None:
            self.observer(step, entry)
        return entry

    def _record(self, result: CycleResult, step: str, outcome: LoopOutcome) -> bool:
        """Journal one step. Returns whether the cycle may continue."""
        payload = outcome.to_dict()
        result.steps.append(self._note(step, payload))
        advanced = payload.get("action") in _EXPECTED[step]
        if not advanced:
            result.stopped_at = step
            result.reason_code = str(payload.get("reason_code") or payload.get("action", ""))
        return advanced

    # -- the cycle -------------------------------------------------------

    def run(
        self,
        *,
        reason: str,
        nonce: str,
        assignment: str,
        verify_token: str,
        expected_artifact: str,
        artifact_path: Optional[Path] = None,
        expected_sender: str = "",
        pm_timeout: float = 900.0,
        worker_timeout: float = 1800.0,
        summary: Optional[str] = None,
    ) -> CycleResult:
        result = CycleResult()

        surface = self.guard.ensure(allow_launch=self.allow_launch)
        result.steps.append(self._note("preflight", surface.to_dict()))
        if not surface.ok:
            result.stopped_at = "preflight"
            result.reason_code = surface.reason_code
            return result

        # 1. Ask PM what to do. Orbit never picks the target itself.
        woken = self.loop.wake_pm(reason=reason, nonce=nonce)
        if not self._record(result, "wake_pm", woken):
            return result

        # 2. Wait for a decision, in the machine-checkable form.
        answered = self.loop.await_directive(timeout=pm_timeout)
        if not self._record(result, "await_directive", answered):
            return result
        directive = PMDirective.from_dict(answered.data["directive"])
        result.directive = directive.to_dict()

        # 3. Carry the assignment to whoever PM named.
        dispatched = self.loop.dispatch(directive=directive, assignment=assignment,
                                        verify_token=verify_token, artifact_path=artifact_path)
        if not self._record(result, "dispatch", dispatched):
            return result
        self.loop.record(
            directive=directive, action=directive.action,
            state_before={"work_state": "awaiting-dispatch"},
            state_after={"delivery_state": dispatched.data.get("delivery_state")},
            evidence={"endpoint": directive.target_endpoint,
                      "artifact": artifact_path.name if artifact_path else ""},
            result="dispatched", classification="success", reason="pm-directed-dispatch")
        self.loop.consume(directive)

        # 4. Wait for the worker. This is the wait a human used to sit through.
        responded = self.loop.await_worker(timeout=worker_timeout)
        if not self._record(result, "await_worker", responded):
            return result

        # 5. Collect the result and validate it before anyone acts on it.
        collected = self.loop.collect(endpoint_id=directive.target_endpoint,
                                      expected_name=expected_artifact,
                                      expected_sender=expected_sender)
        if not self._record(result, "collect", collected):
            return result
        result.artifact = dict(collected.data)

        # 6. Close the loop: report the outcome and open the next question.
        reported = self.loop.report_to_pm(
            summary=summary or f"{expected_artifact} collected and validated",
            nonce=f"{nonce}-report",
            artifact_id=str(collected.data.get("filename", expected_artifact)),
            artifact_digest=str(collected.data.get("sha256", "")))
        if not self._record(result, "report_to_pm", reported):
            return result

        result.completed = True
        result.reason_code = "ok"
        return result


# What each step must produce to keep going. Anything else ends the cycle where
# it stands rather than improvising past it.
_EXPECTED: Dict[str, tuple] = {
    "wake_pm": ("PM_WOKEN",),
    "await_directive": ("DIRECTIVE_ACCEPTED",),
    "dispatch": ("DISPATCHED",),
    "await_worker": ("WORKER_RESPONDED",),
    "collect": ("COLLECTED",),
    "report_to_pm": ("PM_WOKEN",),
}
