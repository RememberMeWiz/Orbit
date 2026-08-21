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
from ..bridge.chatgpt import IDLE_CONFIRM_SECONDS, ChatGptAdapter
from ..bridge.pm_envelope import PMDirective
from ..bridge.registry import ChatEndpointRegistry
from ..bridge.singlewriter import SingleWriterLock
from .assignment import handoff_filename, render as render_assignment, sender_role_for
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


# Failures that mean "not now", not "not ever".
#
# The window is a shared resource: only one conversation is visible, and only
# one runner may transition the delivery ledger. So a lane routinely finds the
# composer mid-stream from another lane's turn, or the ledger held by another
# runner. Treating those as terminal means two lanes sharing one window kill
# each other on contention, which is exactly what a multiplexing supervisor
# exists to avoid.
#
# Found live: lane B woke PM one second after lane A did, hit
# `response-in-progress`, and was marked BLOCKED forever.
TRANSIENT_BLOCKERS = frozenset({
    "response-in-progress",
    "writer-busy",
    "surface-not-ready-in-time",
    "chat-list-not-ready-in-time",
    "focus-verification-failed",
    "composer-not-found",
    "composer-not-present",
})

# A lane that cannot get the window after this many consecutive cycles is not
# contending, it is stuck, and silently retrying forever would hide that.
MAX_CONSECUTIVE_TRANSIENT = 40


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
        # Discovery bookkeeping, reported rather than kept private: an operator
        # needs to see that a lane appeared, and that one is unreadable.
        self.malformed_lanes: Dict[str, str] = {}
        self.discovered_lanes: List[str] = []
        self.vanished_lanes: List[str] = []
        self.last_scan_error = ""

        # Scope comes from the committed configuration and from nowhere else.
        #
        # These were `.get(key, "literal")` defaults. The defaults happened to
        # match the committed values, which is exactly what makes that shape
        # dangerous: a config that lost a key would keep working while silently
        # sourcing scope from a second copy of the truth, and the drift would
        # only surface as an endpoint refusing to resolve much later. A missing
        # key is a broken config, so it fails here instead.
        missing = [key for key in ("project_scope", "workflow_scope", "chat_list_name")
                   if not str(self.config.get(key, "")).strip()]
        if missing:
            raise ValueError(
                f"Orbit endpoint config {self.config_path} is missing required scope "
                f"{', '.join(missing)}; scope must not be defaulted in code")
        self.project_scope = str(self.config["project_scope"])
        self.workflow_scope = str(self.config["workflow_scope"])
        self.chat_list_name = str(self.config["chat_list_name"])

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
        """Full rescan, discarding what was held in memory.

        Kept for startup. Everything running should call `refresh_lanes`, which
        is additive and therefore safe to call in a loop.
        """
        self._lanes.clear()
        self.malformed_lanes = {}
        return self.refresh_lanes()

    def refresh_lanes(self) -> Dict[str, WorkItemLane]:
        """Rescan the lanes directory, additively, every cycle.

        The supervisor used to read the directory once at construction and then
        iterate that snapshot forever, so a lane registered by any other process
        after startup was invisible until someone restarted it. A process that is
        alive but cannot see new work is not autonomous, so discovery is durable
        and periodic rather than a one-time load.

        Additive on purpose. A directory that is briefly unreadable -- an
        antivirus scan, a half-written save, a sync client -- must not be able to
        erase known work from a running supervisor, so lanes already in memory
        are kept when they vanish from the scan and the disappearance is
        recorded instead.

        A lane that will not parse is held aside as malformed rather than
        reinitialised, because rewriting a state file Orbit failed to understand
        is how in-flight work gets silently restarted.
        """
        if not self.lanes_dir.exists():
            return self._lanes

        try:
            entries = sorted(e for e in self.lanes_dir.iterdir() if e.is_dir())
        except OSError as exc:
            self.last_scan_error = f"{type(exc).__name__}: {exc}"
            return self._lanes
        self.last_scan_error = ""

        seen = set()
        for entry in entries:
            try:
                lane = WorkItemLane(entry, global_stop_path=self.global_stop_path)
                lane.load_record()
                # The record's own claim, compared against the directory that
                # names it. The directory is what routing and the inbox use.
                work_item = lane.record.work_item
            except Exception as exc:
                # One unreadable lane must not stop every healthy one.
                self.malformed_lanes[entry.name] = f"{type(exc).__name__}: {exc}"
                continue

            if not work_item:
                self.malformed_lanes[entry.name] = "lane-record-has-no-work-item"
                continue
            if work_item != entry.name:
                # The directory name is the identity used for routing and for
                # the inbox, so a record claiming a different one is ambiguous.
                self.malformed_lanes[entry.name] = (
                    f"lane-identity-mismatch: directory {entry.name}, record {work_item}")
                continue

            seen.add(work_item)
            self.malformed_lanes.pop(entry.name, None)
            existing = self._lanes.get(work_item)
            if existing is None:
                self._lanes[work_item] = lane
                self.discovered_lanes.append(work_item)
            else:
                # Reload the authoritative record; another process may have
                # advanced it, and disk is the authority.
                existing.load_record()

        self.vanished_lanes = sorted(set(self._lanes) - seen)
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

    def _note_failure(self, lane: "WorkItemLane", reason_code: str, detail: str,
                      action: str, retry_state: str) -> Dict[str, Any]:
        """Block on a real failure; wait and retry on contention.

        The distinction is the whole point of multiplexing. A lane that finds
        the window busy has not failed, it has simply not had its turn yet, and
        blocking it there would mean the busier the system the more lanes die.
        A lane that cannot get its turn for many consecutive cycles is a
        different thing, and does block, so a genuine stall is still visible.
        """
        rec = lane.record
        rec.blocker_code = reason_code
        rec.blocker_detail = detail

        if reason_code in TRANSIENT_BLOCKERS:
            rec.transient_count += 1
            if rec.transient_count < MAX_CONSECUTIVE_TRANSIENT:
                rec.work_state = retry_state
                lane.save_record()
                return {"work_item": lane.work_item, "action": "WAITING_FOR_TURN",
                        "state": rec.work_state, "reason_code": reason_code,
                        "attempts": rec.transient_count}
            detail = f"{detail} (after {rec.transient_count} consecutive attempts)".strip()
            rec.blocker_detail = detail

        rec.work_state = STATE_BLOCKED
        lane.save_record()
        return {"work_item": lane.work_item, "action": action, "state": rec.work_state,
                "reason_code": reason_code}

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
                rec.transient_count = 0
                lane.save_record()
                return {"work_item": lane.work_item, "action": "PM_WOKEN", "state": rec.work_state}
            else:
                return self._note_failure(lane, out.reason_code, out.detail,
                                          "WAKE_FAILED", STATE_INITIALIZED)

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

            # The expected handoff name and the assignment must agree, and
            # neither can be known until PM has chosen the endpoint -- the role
            # that signs the handoff is the role PM routed to. So both are
            # settled here, once, and the name is persisted so collection later
            # looks for exactly what the worker was asked for.
            sender_role = rec.expected_sender or sender_role_for(target_ep, self.adapter.registry)
            if not rec.expected_handoff:
                rec.expected_handoff = handoff_filename(lane.work_item, sender_role)
                rec.expected_sender = sender_role
                lane.save_record()

            if rec.assignment_path and Path(rec.assignment_path).is_file():
                assignment_text = Path(rec.assignment_path).read_text(encoding="utf-8")
            else:
                # Previously "Assignment for W: <objective>" -- a task with no
                # reply contract at all, so the answer could never be collected
                # and the lane blocked later looking like the worker's fault.
                assignment_text = render_assignment(
                    lane.work_item, rec.objective,
                    sender_role=sender_role,
                    token=rec.verify_token or lane.work_item,
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
                rec.transient_count = 0
                lane.save_record()
                return {"work_item": lane.work_item, "action": "DISPATCHED", "state": rec.work_state}
            else:
                return self._note_failure(lane, out.reason_code, out.detail,
                                          "DISPATCH_FAILED", rec.work_state)

        # 4. AWAITING_WORKER -> Check response
        if rec.work_state == STATE_AWAITING_WORKER:
            focused = self.adapter.focus(rec.current_endpoint)
            if not focused.ok:
                return {"work_item": lane.work_item, "action": "WORKER_FOCUS_FAILED", "state": rec.work_state}

            # Is the answer there yet? -- not -- did I witness it arriving?
            #
            # This required seeing streaming and then seeing idle hold. That is
            # edge-triggered, and the supervisor samples every ~30s because it
            # is sharing the window between lanes, so a worker that answers in
            # twenty seconds finishes entirely between two samples. Observed
            # live: both lanes sat with saw_streaming=false and observed=idle
            # forever, waiting for a phase that had already been and gone.
            #
            # So the wait is level-triggered on the thing actually wanted. Idle
            # means "try collecting"; the handoff is either in the transcript or
            # it is not. A partially written answer cannot be mistaken for a
            # finished one because collection requires the closing marker, so a
            # half-streamed block is simply not found yet.
            state = self.adapter.driver.response_state()
            if not state.ok:
                return {"work_item": lane.work_item, "action": "WORKER_STATE_UNREADABLE",
                        "state": rec.work_state, "reason_code": state.reason_code}

            observed = str(state.data.get("state", "unknown"))
            if observed == "streaming":
                rec.saw_streaming = True
                lane.save_record()
                return {"work_item": lane.work_item, "action": "AWAITING_WORKER_RESPONSE",
                        "state": rec.work_state, "observed": observed}

            rec.work_state = STATE_COLLECTING
            lane.save_record()
            return {"work_item": lane.work_item, "action": "WORKER_IDLE_TRY_COLLECT",
                    "state": rec.work_state, "observed": observed}

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
            elif out.reason_code in ("transcript-handoff-not-found",
                                     "artifact-not-present"):
                # Not a failure: the worker has not finished writing. Back to
                # waiting, and the next cycle asks again.
                rec.work_state = STATE_AWAITING_WORKER
                lane.save_record()
                return {"work_item": lane.work_item, "action": "AWAITING_WORKER_RESPONSE",
                        "state": rec.work_state, "reason_code": out.reason_code}
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
        """Run one pass across all active lanes, rescanning first."""
        self.refresh_lanes()
        results: List[Dict[str, Any]] = []
        for lane in self.list_lanes():
            if not lane.stopped() and not lane.paused():
                res = self.step_lane(lane)
                results.append(res)
        return results

    def status_summary(self) -> Dict[str, Any]:
        # refresh, not load: load clears memory first, which would let a
        # momentarily unreadable directory erase lanes from a running
        # supervisor. Status and cycle must see the same inventory.
        self.refresh_lanes()
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
