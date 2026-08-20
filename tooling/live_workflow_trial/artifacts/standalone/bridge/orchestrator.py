"""PM-supervised apprenticeship loop.

Ties the pieces into one governed cycle:

    wake PM → await directive → execute it → collect result → report → wait

Two rules hold throughout, and everything else follows from them:

* PM decides routing. Orbit never picks the next hop by itself during the
  apprenticeship phase, so every dispatch traces back to a directive that
  quoted the pending request id.
* Nothing external happens twice. Dispatches go through the durable delivery
  ledger, so a crash at any point either retries safely or reports AMBIGUOUS.

The loop reports what happened; it never reinterprets a worker's BLOCKED or a
PM's silence as progress.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .chatgpt import ChatGptAdapter
from .delivery import DeliveryLedger, digest_text
from .pm_envelope import PMBridgeState, PMDirective, PMRequest, request_identity
from .teaching import TeachingTrace, TeachingTraceStore, condition_digest

# How long to wait for a human PM to answer before reporting that we are still
# waiting. Not an error: PM is a person, and waiting is the correct state.
DEFAULT_PM_TIMEOUT = 1800.0
PM_POLL_SECONDS = 20.0


@dataclass
class LoopOutcome:
    action: str
    reason_code: str = ""
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "reason_code": self.reason_code,
                "detail": self.detail, "data": dict(self.data)}


class ApprenticeLoop:
    def __init__(
        self,
        *,
        adapter: ChatGptAdapter,
        pm_state: PMBridgeState,
        ledger: DeliveryLedger,
        traces: TeachingTraceStore,
        work_item: str,
        pm_endpoint_id: str = "orbit-pm",
        inbox_dir: Optional[Path] = None,
        stop_path: Optional[Path] = None,
        sleeper: Callable[[float], None] = None,
        clock: Callable[[], float] = None,
    ):
        import time as _time
        self.adapter = adapter
        self.pm_state = pm_state
        self.ledger = ledger
        self.traces = traces
        self.work_item = work_item
        self.pm_endpoint_id = pm_endpoint_id
        self.inbox_dir = Path(inbox_dir) if inbox_dir else None
        self.stop_path = Path(stop_path) if stop_path else None
        self._sleep = sleeper or _time.sleep
        self._now = clock or _time.monotonic

    # -- helpers ---------------------------------------------------------

    def stopped(self) -> bool:
        return bool(self.stop_path and self.stop_path.is_file())

    def _machine_note(self) -> str:
        return ("\n\nPosted by the Orbit local program through the ChatGPT desktop "
                "accessibility bridge. No file was carried by the Product Owner.\n"
                "Reply with an ORBIT_DIRECTIVE envelope quoting the request_id above.")

    # -- wake PM ---------------------------------------------------------

    def wake_pm(
        self,
        *,
        reason: str,
        nonce: str,
        current_owner: str = "ORBIT",
        workflow_state: Optional[Dict[str, Any]] = None,
        artifact_id: str = "",
        artifact_digest: str = "",
        safe_actions: tuple = ("DISPATCH_TO_ROLE", "HOLD", "STOP"),
    ) -> LoopOutcome:
        """Post a machine-generated request into the PM conversation."""
        if self.stopped():
            return LoopOutcome("STOPPED", "stop-active")

        request = PMRequest(
            request_id=request_identity(self.work_item, reason, nonce),
            work_item=self.work_item, reason=reason, current_owner=current_owner,
            workflow_state=workflow_state or {}, artifact_id=artifact_id,
            artifact_digest=artifact_digest, safe_actions=safe_actions,
        )
        body = request.render() + self._machine_note()

        result = self.adapter.deliver(
            ledger=self.ledger,
            request_id=f"pm-{request.request_id}",
            endpoint_id=self.pm_endpoint_id,
            message=body,
            verify_token=request.request_id,
            stop_path=self.stop_path,
        )
        if not result.ok:
            return LoopOutcome("WAKE_FAILED", result.reason_code, result.detail,
                               {"request_id": request.request_id})

        # Only record the request as pending once it is actually on its way, so
        # a failed post does not leave Orbit waiting for an answer to a question
        # PM never received.
        self.pm_state.open_request(request)
        return LoopOutcome("PM_WOKEN", "request-posted", "",
                           {"request_id": request.request_id, "reason": reason})

    # -- await directive -------------------------------------------------

    def await_directive(self, *, timeout: float = DEFAULT_PM_TIMEOUT) -> LoopOutcome:
        """Poll the PM conversation until a valid directive appears."""
        started = self._now()
        last_reason = "directive-absent"
        while True:
            if self.stopped():
                return LoopOutcome("STOPPED", "stop-active")

            focused = self.adapter.focus(self.pm_endpoint_id)
            if not focused.ok:
                return LoopOutcome("PM_UNREACHABLE", focused.reason_code, focused.detail)

            tail = self.adapter.driver.read_transcript_tail(8000)
            if tail.ok:
                verdict = self.pm_state.evaluate(str(tail.data.get("text", "")))
                if verdict.accepted:
                    return LoopOutcome("DIRECTIVE_ACCEPTED", verdict.reason_code, verdict.detail,
                                       {"directive": verdict.directive.to_dict()})
                last_reason = verdict.reason_code
                # A stale or malformed envelope is worth surfacing immediately
                # rather than silently waiting out the whole timeout on it.
                if last_reason in ("directive-stale-request-id", "directive-work-item-mismatch",
                                   "directive-already-consumed"):
                    return LoopOutcome("DIRECTIVE_REJECTED", last_reason, verdict.detail)

            if (self._now() - started) >= timeout:
                return LoopOutcome("AWAITING_PM", last_reason,
                                   f"no directive after {timeout:.0f}s")
            self._sleep(PM_POLL_SECONDS)

    # -- execute ---------------------------------------------------------

    def dispatch(
        self,
        *,
        directive: PMDirective,
        assignment: str,
        verify_token: str,
        artifact_path: Optional[Path] = None,
        expected_sha256: str = "",
    ) -> LoopOutcome:
        """Carry out DISPATCH_TO_ROLE against the endpoint PM named."""
        if self.stopped():
            return LoopOutcome("STOPPED", "stop-active")
        if directive.action != "DISPATCH_TO_ROLE":
            return LoopOutcome("UNSUPPORTED_ACTION", directive.action)
        if not directive.target_endpoint:
            return LoopOutcome("DIRECTIVE_INCOMPLETE", "directive-missing-target-endpoint")

        request_id = f"dispatch-{directive.directive_id}"
        result = self.adapter.deliver(
            ledger=self.ledger,
            request_id=request_id,
            endpoint_id=directive.target_endpoint,
            message=assignment,
            verify_token=verify_token,
            artifact_path=artifact_path,
            expected_sha256=expected_sha256,
            stop_path=self.stop_path,
        )
        if not result.ok:
            return LoopOutcome("DISPATCH_FAILED", result.reason_code, result.detail,
                               {"request_id": request_id, "delivery_state": result.delivery_state})
        return LoopOutcome("DISPATCHED", "sent", "", {
            "request_id": request_id,
            "endpoint_id": directive.target_endpoint,
            "delivery_state": result.delivery_state,
        })

    def await_worker(self, *, timeout: float = 900.0) -> LoopOutcome:
        obs = self.adapter.wait_for_response(timeout=timeout)
        if obs.state == "complete":
            return LoopOutcome("WORKER_RESPONDED", "complete", obs.detail,
                               {"elapsed": obs.elapsed, "polls": obs.polls})
        return LoopOutcome("WORKER_NOT_READY", obs.state, obs.detail,
                           {"elapsed": obs.elapsed, "polls": obs.polls})

    def collect(self, *, endpoint_id: str, expected_name: str,
                expected_sender: str = "") -> LoopOutcome:
        if self.inbox_dir is None:
            return LoopOutcome("COLLECT_FAILED", "no-bridge-inbox-configured")
        result = self.adapter.collect_artifact(
            endpoint_id=endpoint_id, expected_name=expected_name,
            inbox_dir=self.inbox_dir, work_item=self.work_item,
            expected_sender=expected_sender)
        if not result.ok:
            return LoopOutcome("COLLECT_FAILED", result.reason_code, result.detail)
        return LoopOutcome("COLLECTED", "validated", "", dict(result.data))

    # -- report ----------------------------------------------------------

    def report_to_pm(self, *, summary: str, nonce: str,
                     artifact_id: str = "", artifact_digest: str = "",
                     safe_actions: tuple = ("DISPATCH_TO_ROLE", "HOLD", "STOP")) -> LoopOutcome:
        """Report an outcome and open the next question in one message."""
        return self.wake_pm(reason=summary, nonce=nonce, current_owner="ORBIT",
                            artifact_id=artifact_id, artifact_digest=artifact_digest,
                            safe_actions=safe_actions)

    # -- traces ----------------------------------------------------------

    def record(self, *, directive: PMDirective, action: str, state_before: Dict[str, Any],
               state_after: Dict[str, Any], evidence: Dict[str, Any], result: str,
               classification: str, reason: str, owner: str = "ORBIT") -> Dict[str, Any]:
        return self.traces.append(TeachingTrace(
            work_item=self.work_item,
            pm_request_id=directive.request_id,
            directive_id=directive.directive_id,
            action=action,
            condition_digest=condition_digest(
                work_item=self.work_item, owner=owner,
                work_state=str(state_before.get("work_state", "")), reason=reason),
            state_before=state_before, state_after=state_after,
            evidence=evidence, result=result, classification=classification,
        ))

    def consume(self, directive: PMDirective) -> None:
        self.pm_state.consume(directive)
