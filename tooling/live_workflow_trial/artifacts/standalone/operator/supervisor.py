"""Multi-Work-Item Supervisor for Orbit.

Supervises multiple independent workflow lanes concurrently:
- Reconstructs all lanes from durable state on startup.
- Enforces strict isolation: work items never share mutable state or consume
  each other's directives.
- One lane blocking or holding never freezes unrelated safe lanes.
- Enforces PM routing authority before any worker dispatch.
- Preserves exact PM directive semantics (HOLD, STOP, DISPATCH_TO_ROLE).
- Uses SingleWriterLock for exclusive local UIA actuation.
- Deduplicates PM wake notifications.
- Dynamically loads project and workflow scope from committed Orbit config.
- Persists all progress, traces, and metrics.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..bridge.accessibility import AccessibilityGuard
from ..bridge.chatgpt import ChatGptAdapter
from ..bridge.pm_envelope import PMDirective
from ..bridge.registry import ChatEndpointRegistry
from ..bridge.singlewriter import SingleWriterLock
from .lane import (
    STATE_AWAITING_PM_ROUTING,
    STATE_AWAITING_WORKER,
    STATE_BLOCKED,
    STATE_COLLECTING,
    STATE_COMPLETED,
    STATE_DIRECTIVE_ACCEPTED,
    STATE_DISPATCHING,
    STATE_HOLD,
    STATE_INITIALIZED,
    STATE_PAUSED,
    STATE_REPORTING_TO_PM,
    STATE_STOPPED,
    LaneRecord,
    WorkItemLane,
    utc_now_iso,
)
from .telemetry import HopTelemetry, TelemetryStore

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "bridge" / "orbit_endpoints.json"


def load_orbit_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    target = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not target.is_file():
        raise FileNotFoundError(f"Orbit endpoint config not found at: {target}")
    return json.loads(target.read_text(encoding="utf-8"))


class MultiWorkItemSupervisor:
    """Supervises and multiplexes multiple independent work-item lanes."""

    def __init__(
        self,
        state_dir: Path,
        adapter: Optional[ChatGptAdapter] = None,
        *,
        config_path: Optional[Path] = None,
        driver_timeout: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lanes_dir = self.state_dir / "lanes"
        self.lanes_dir.mkdir(parents=True, exist_ok=True)
        self.global_stop_path = self.state_dir / "STOP"
        self.telemetry = TelemetryStore(self.state_dir / "telemetry.jsonl")
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.config = load_orbit_config(self.config_path)
        self.driver_timeout = driver_timeout
        self._clock = clock
        self._sleep = sleeper

        # Configured scope parameters directly from committed configuration
        self.project_scope = str(self.config.get("project_scope", "Orbit"))
        self.workflow_scope = str(self.config.get("workflow_scope", "orbit-m0-live-trial"))
        self.chat_list_name = str(self.config.get("chat_list_name", "Chats in Yong 2"))

        if adapter is not None:
            self.adapter = adapter
        else:
            registry = ChatEndpointRegistry.from_orbit_config(self.config_path)
            self.adapter = ChatGptAdapter(
                registry,
                project_scope=self.project_scope,
                workflow_scope=self.workflow_scope,
                chat_list_name=self.chat_list_name,
            )
            self.adapter.driver.timeout = driver_timeout

        self._lanes: Dict[str, WorkItemLane] = {}
        self.load_lanes()

    def stopped(self) -> bool:
        return bool(self.global_stop_path.is_file())

    def stop_all(self) -> None:
        self.global_stop_path.touch(exist_ok=True)
        for lane in self._lanes.values():
            lane.stop()

    def resume_all(self) -> None:
        if self.global_stop_path.is_file():
            self.global_stop_path.unlink()
        for lane in self._lanes.values():
            if lane.paused():
                lane.resume()

    def load_lanes(self) -> Dict[str, WorkItemLane]:
        self._lanes.clear()
        if not self.lanes_dir.exists():
            return self._lanes
        for entry in self.lanes_dir.iterdir():
            if entry.is_dir():
                lane = WorkItemLane(entry, global_stop_path=self.global_stop_path)
                self._lanes[lane.work_item] = lane
        return self._lanes

    def get_lane(self, work_item: str) -> Optional[WorkItemLane]:
        return self._lanes.get(work_item)

    def list_lanes(self) -> List[WorkItemLane]:
        return list(self._lanes.values())

    def create_lane(
        self,
        work_item: str,
        objective: str,
        *,
        assignment_path: str = "",
        artifact_path: str = "",
        expect: str = "",
        sender: str = "",
        token: str = "",
        source: str = "transcript",
        nonce: str = "",
    ) -> WorkItemLane:
        lane_dir = self.lanes_dir / work_item
        lane = WorkItemLane(lane_dir, global_stop_path=self.global_stop_path)
        lane.record.objective = objective
        lane.record.assignment_path = assignment_path
        lane.record.artifact_path = artifact_path
        lane.record.expected_handoff = expect
        lane.record.expected_sender = sender
        lane.record.verify_token = token or work_item
        lane.record.source = source
        lane.record.nonce = nonce or f"{work_item}-{int(time.time())}"
        lane.record.work_state = STATE_INITIALIZED
        lane.save_record()
        self._lanes[work_item] = lane
        return lane

    def check_surface(self, *, allow_launch: bool = True) -> Dict[str, Any]:
        guard = AccessibilityGuard(
            self.adapter.driver,
            chat_list_name=self.adapter.chat_list_name,
        )
        outcome = guard.ensure(allow_launch=allow_launch)
        return outcome.to_dict()

    def step_lane(self, lane: WorkItemLane) -> Dict[str, Any]:
        """Execute one state step for an individual lane."""
        lane.load_record()
        rec = lane.record

        if lane.stopped():
            return {"work_item": lane.work_item, "action": "STOPPED", "state": STATE_STOPPED}
        if lane.paused():
            return {"work_item": lane.work_item, "action": "PAUSED", "state": STATE_PAUSED}
        if rec.work_state in (STATE_COMPLETED, STATE_BLOCKED, STATE_HOLD):
            return {"work_item": lane.work_item, "action": "IDLE", "state": rec.work_state}

        loop = lane.build_loop(self.adapter)

        # 1. INITIALIZED -> Wake PM for routing
        if rec.work_state == STATE_INITIALIZED:
            reason = f"Routing request for work item: {rec.objective or rec.work_item}"
            out = loop.wake_pm(reason=reason, nonce=rec.nonce)
            if out.action == "PM_WOKEN":
                rec.pending_request_id = out.data.get("request_id", "")
                rec.work_state = STATE_AWAITING_PM_ROUTING
                rec.current_endpoint = "orbit-pm"
                lane.save_record()
                return {"work_item": lane.work_item, "action": "PM_WOKEN", "state": rec.work_state}
            else:
                rec.blocker_code = out.reason_code
                rec.blocker_detail = out.detail
                rec.work_state = STATE_BLOCKED
                lane.save_record()
                return {"work_item": lane.work_item, "action": "WAKE_FAILED", "state": rec.work_state}

        # 2. AWAITING_PM_ROUTING -> Poll PM for directive
        if rec.work_state == STATE_AWAITING_PM_ROUTING:
            focused = self.adapter.focus(loop.pm_endpoint_id)
            if not focused.ok:
                return {"work_item": lane.work_item, "action": "PM_FOCUS_FAILED", "state": rec.work_state}

            tail = self.adapter.driver.read_transcript_tail(8000)
            if tail.ok:
                verdict = loop.pm_state.evaluate(str(tail.data.get("text", "")))
                if verdict.accepted and verdict.directive:
                    directive: PMDirective = verdict.directive
                    # Enforce strict work item matching
                    if directive.work_item == lane.work_item:
                        rec.accepted_directive_id = directive.directive_id
                        rec.accepted_action = directive.action
                        loop.consume(directive)

                        # EXACT PM DIRECTIVE SEMANTICS PRESERVATION
                        if directive.action == "HOLD":
                            rec.work_state = STATE_HOLD
                            lane.save_record()
                            loop.record(
                                directive=directive,
                                action="HOLD",
                                state_before={"work_state": STATE_AWAITING_PM_ROUTING},
                                state_after={"work_state": STATE_HOLD},
                                evidence={"notes": directive.notes},
                                result="held",
                                classification="success",
                                reason="pm-directed-hold",
                            )
                            return {
                                "work_item": lane.work_item,
                                "action": "PM_DIRECTED_HOLD",
                                "directive": directive.to_dict(),
                                "state": rec.work_state,
                            }

                        elif directive.action == "STOP":
                            lane.stop()
                            loop.record(
                                directive=directive,
                                action="STOP",
                                state_before={"work_state": STATE_AWAITING_PM_ROUTING},
                                state_after={"work_state": STATE_STOPPED},
                                evidence={"notes": directive.notes},
                                result="stopped",
                                classification="success",
                                reason="pm-directed-stop",
                            )
                            return {
                                "work_item": lane.work_item,
                                "action": "PM_DIRECTED_STOP",
                                "directive": directive.to_dict(),
                                "state": rec.work_state,
                            }

                        elif directive.action == "DISPATCH_TO_ROLE":
                            rec.current_endpoint = directive.target_endpoint
                            rec.work_state = STATE_DIRECTIVE_ACCEPTED
                            lane.save_record()
                            return {
                                "work_item": lane.work_item,
                                "action": "DIRECTIVE_ACCEPTED",
                                "directive": directive.to_dict(),
                                "state": rec.work_state,
                            }
                        else:
                            rec.work_state = STATE_BLOCKED
                            rec.blocker_code = f"unsupported-directive-action:{directive.action}"
                            lane.save_record()
                            return {
                                "work_item": lane.work_item,
                                "action": "UNSUPPORTED_DIRECTIVE_ACTION",
                                "state": rec.work_state,
                            }

            return {"work_item": lane.work_item, "action": "AWAITING_PM_DIRECTIVE", "state": rec.work_state}

        # 3. DIRECTIVE_ACCEPTED / DISPATCHING -> Dispatch only if action is DISPATCH_TO_ROLE
        if rec.work_state in (STATE_DIRECTIVE_ACCEPTED, STATE_DISPATCHING):
            if rec.accepted_action != "DISPATCH_TO_ROLE":
                rec.work_state = STATE_BLOCKED
                rec.blocker_code = f"cannot-dispatch-non-dispatch-action:{rec.accepted_action}"
                lane.save_record()
                return {"work_item": lane.work_item, "action": "DISPATCH_REFUSED", "state": rec.work_state}

            directive_id = rec.accepted_directive_id
            target_ep = rec.current_endpoint
            if not target_ep:
                rec.work_state = STATE_BLOCKED
                rec.blocker_code = "missing-target-endpoint"
                lane.save_record()
                return {"work_item": lane.work_item, "action": "DISPATCH_BLOCKED", "state": rec.work_state}

            assignment_text = (
                Path(rec.assignment_path).read_text(encoding="utf-8")
                if rec.assignment_path and Path(rec.assignment_path).is_file()
                else f"Assignment for {rec.work_item}: {rec.objective}"
            )
            artifact_file = Path(rec.artifact_path) if rec.artifact_path and Path(rec.artifact_path).is_file() else None

            # Reconstruct directive object with exact preserved action
            directive = PMDirective(
                directive_id=directive_id,
                request_id=rec.pending_request_id,
                work_item=lane.work_item,
                action=rec.accepted_action,
                target_endpoint=target_ep,
            )

            with SingleWriterLock(lane.delivery_path):
                out = loop.dispatch(
                    directive=directive,
                    assignment=assignment_text,
                    verify_token=rec.verify_token or lane.work_item,
                    artifact_path=artifact_file,
                )

            if out.action == "DISPATCHED":
                loop.record(
                    directive=directive,
                    action=directive.action,
                    state_before={"work_state": STATE_DIRECTIVE_ACCEPTED},
                    state_after={"delivery_state": out.data.get("delivery_state")},
                    evidence={"endpoint": target_ep},
                    result="dispatched",
                    classification="success",
                    reason="pm-directed-dispatch",
                )
                rec.work_state = STATE_AWAITING_WORKER
                lane.save_record()
                return {"work_item": lane.work_item, "action": "DISPATCHED", "state": rec.work_state}
            else:
                rec.work_state = STATE_BLOCKED
                rec.blocker_code = out.reason_code
                rec.blocker_detail = out.detail
                lane.save_record()
                return {"work_item": lane.work_item, "action": "DISPATCH_FAILED", "state": rec.work_state}

        # 4. AWAITING_WORKER -> Check response
        if rec.work_state == STATE_AWAITING_WORKER:
            focused = self.adapter.focus(rec.current_endpoint)
            if not focused.ok:
                return {"work_item": lane.work_item, "action": "WORKER_FOCUS_FAILED", "state": rec.work_state}

            obs = self.adapter.wait_for_response(timeout=0.0)
            if obs.state == "complete":
                rec.work_state = STATE_COLLECTING
                lane.save_record()
                return {"work_item": lane.work_item, "action": "WORKER_RESPONDED", "state": rec.work_state}
            return {"work_item": lane.work_item, "action": "AWAITING_WORKER_RESPONSE", "state": rec.work_state}

        # 5. COLLECTING -> Collect handoff
        if rec.work_state == STATE_COLLECTING:
            expect_name = rec.expected_handoff or f"HANDOFF_{rec.work_item}.md"
            out = loop.collect(
                endpoint_id=rec.current_endpoint,
                expected_name=expect_name,
                expected_sender=rec.expected_sender,
                source=rec.source,
            )
            if out.action == "COLLECTED":
                rec.result_digest = str(out.data.get("sha256", ""))
                rec.work_state = STATE_REPORTING_TO_PM
                lane.save_record()
                return {"work_item": lane.work_item, "action": "COLLECTED", "state": rec.work_state}
            else:
                rec.work_state = STATE_BLOCKED
                rec.blocker_code = out.reason_code
                rec.blocker_detail = out.detail
                lane.save_record()
                return {"work_item": lane.work_item, "action": "COLLECT_FAILED", "state": rec.work_state}

        # 6. REPORTING_TO_PM -> Report to PM and complete
        if rec.work_state == STATE_REPORTING_TO_PM:
            summary = f"Completed {rec.work_item}: {rec.expected_handoff or 'result'} collected"
            out = loop.report_to_pm(
                summary=summary,
                nonce=f"{rec.nonce}-report",
                artifact_id=rec.expected_handoff,
                artifact_digest=rec.result_digest,
            )
            if out.action == "PM_WOKEN":
                rec.work_state = STATE_COMPLETED
                lane.save_record()
                # Record telemetry
                self.telemetry.record(
                    HopTelemetry(
                        work_item=lane.work_item,
                        hop_id=f"hop-{rec.work_item}",
                        target_endpoint=rec.current_endpoint,
                        pm_request_id=rec.pending_request_id,
                        directive_id=rec.accepted_directive_id,
                        result="SUCCESS",
                    )
                )
                return {"work_item": lane.work_item, "action": "REPORTED_TO_PM", "state": rec.work_state}
            else:
                rec.work_state = STATE_BLOCKED
                rec.blocker_code = out.reason_code
                rec.blocker_detail = out.detail
                lane.save_record()
                return {"work_item": lane.work_item, "action": "REPORT_FAILED", "state": rec.work_state}

        return {"work_item": lane.work_item, "action": "NO_OP", "state": rec.work_state}

    def cycle_all(self) -> List[Dict[str, Any]]:
        """Run one pass across all active lanes."""
        results: List[Dict[str, Any]] = []
        for lane in self.list_lanes():
            if not lane.stopped() and not lane.paused():
                res = self.step_lane(lane)
                results.append(res)
        return results

    def status_summary(self) -> Dict[str, Any]:
        self.load_lanes()
        surface = self.check_surface(allow_launch=False)
        lane_summaries = [l.summary_dict() for l in self.list_lanes()]
        active_count = sum(1 for l in lane_summaries if l["work_state"] not in (STATE_COMPLETED, STATE_BLOCKED, STATE_STOPPED, STATE_HOLD))
        blocked_count = sum(1 for l in lane_summaries if l["work_state"] == STATE_BLOCKED)
        completed_count = sum(1 for l in lane_summaries if l["work_state"] == STATE_COMPLETED)
        hold_count = sum(1 for l in lane_summaries if l["work_state"] == STATE_HOLD)

        return {
            "stopped": self.stopped(),
            "surface": surface,
            "project_scope": self.project_scope,
            "workflow_scope": self.workflow_scope,
            "chat_list_name": self.chat_list_name,
            "total_lanes": len(lane_summaries),
            "active_lanes": active_count,
            "blocked_lanes": blocked_count,
            "completed_lanes": completed_count,
            "hold_lanes": hold_count,
            "lanes": lane_summaries,
            "telemetry_summary": self.telemetry.summary(),
        }
