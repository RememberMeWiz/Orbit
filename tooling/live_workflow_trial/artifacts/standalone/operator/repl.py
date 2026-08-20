"""Interactive Operator Console / REPL for Orbit.

Provides a lightweight, human-friendly console for the Product Owner:
- Inspect overall health & lane statuses
- Register new objectives (which request PM routing, never arbitrary execution)
- Pause, resume, and stop lanes
- Step lanes individually or trigger cycles
- Query workflow metrics and self-improvement insights

Rule preserved:
    conversation / objective != execution authorization
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .insights import WorkflowInsightsAnalyzer
from .supervisor import MultiWorkItemSupervisor


class OperatorRepl:
    """Interactive operator shell for Orbit."""

    def __init__(
        self,
        supervisor: MultiWorkItemSupervisor,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ):
        self.supervisor = supervisor
        self._input = input_fn
        self._output = output_fn
        self.insights = WorkflowInsightsAnalyzer(self.supervisor.telemetry)

    def print_banner(self) -> None:
        self._output("============================================================")
        self._output("                ORBIT OPERATOR CONSOLE                      ")
        self._output("============================================================")
        self._output("Type 'help' for commands, 'status' for system health, 'quit' to exit.")
        self._output("")

    def cmd_status(self, args: List[str]) -> None:
        summary = self.supervisor.status_summary()
        surface = summary.get("surface", {})
        self._output("--- System Health ---")
        self._output(f"Surface Status     : {surface.get('status', 'UNKNOWN')} (ok={surface.get('ok')}, drivable={surface.get('drivable')})")
        if surface.get("remedy"):
            self._output(f"Remedy             : {surface.get('remedy')}")
        self._output(f"Global STOP Active : {summary.get('stopped', False)}")
        self._output(f"Total Lanes        : {summary.get('total_lanes', 0)} (Active: {summary.get('active_lanes', 0)}, Blocked: {summary.get('blocked_lanes', 0)}, Completed: {summary.get('completed_lanes', 0)})")
        self._output("")

    def cmd_lanes(self, args: List[str]) -> None:
        lanes = self.supervisor.list_lanes()
        if not lanes:
            self._output("No active or registered work-item lanes.")
            return

        self._output(f"{'WORK ITEM':<16} {'STATE':<22} {'ENDPOINT':<18} {'OBJECTIVE':<30}")
        self._output("-" * 90)
        for lane in lanes:
            rec = lane.record
            obj = (rec.objective[:27] + "...") if len(rec.objective) > 30 else rec.objective
            self._output(f"{rec.work_item:<16} {rec.work_state:<22} {rec.current_endpoint or 'none':<18} {obj:<30}")
        self._output("")

    def cmd_show(self, args: List[str]) -> None:
        if not args:
            self._output("Usage: show <work_item>")
            return
        work_item = args[0]
        lane = self.supervisor.get_lane(work_item)
        if not lane:
            self._output(f"Error: Lane '{work_item}' not found.")
            return
        rec = lane.record
        self._output(f"--- Lane: {rec.work_item} ---")
        self._output(f"Objective          : {rec.objective}")
        self._output(f"Work State         : {rec.work_state}")
        self._output(f"Current Endpoint   : {rec.current_endpoint or 'none'}")
        self._output(f"Pending Request ID : {rec.pending_request_id or 'none'}")
        self._output(f"Accepted Directive : {rec.accepted_directive_id or 'none'}")
        self._output(f"Expected Handoff   : {rec.expected_handoff or 'none'}")
        self._output(f"Last Progress At   : {rec.last_progress_at}")
        self._output(f"Stopped            : {lane.stopped()}")
        if rec.blocker_code:
            self._output(f"Blocker Code       : {rec.blocker_code}")
            self._output(f"Blocker Detail     : {rec.blocker_detail}")
        if rec.result_digest:
            self._output(f"Result Digest      : {rec.result_digest}")
        self._output("")

    def cmd_work(self, args: List[str]) -> None:
        if not args:
            self._output("Usage: work <objective text>")
            return
        objective = " ".join(args)
        import time as _t
        work_item = f"WORK-{int(_t.time()) % 100000:05d}"
        lane = self.supervisor.create_lane(work_item, objective)
        self._output(f"Registered new work item: {work_item}")
        self._output("Requesting PM routing...")
        step_res = self.supervisor.step_lane(lane)
        self._output(f"Action: {step_res.get('action')}, State: {step_res.get('state')}")
        self._output("")

    def cmd_pause(self, args: List[str]) -> None:
        if not args:
            self._output("Usage: pause <work_item>")
            return
        work_item = args[0]
        lane = self.supervisor.get_lane(work_item)
        if not lane:
            self._output(f"Error: Lane '{work_item}' not found.")
            return
        lane.pause()
        self._output(f"Lane '{work_item}' paused.")

    def cmd_resume(self, args: List[str]) -> None:
        if not args:
            self._output("Usage: resume <work_item>")
            return
        work_item = args[0]
        lane = self.supervisor.get_lane(work_item)
        if not lane:
            self._output(f"Error: Lane '{work_item}' not found.")
            return
        lane.resume()
        self._output(f"Lane '{work_item}' resumed.")

    def cmd_stop(self, args: List[str]) -> None:
        if not args:
            self.supervisor.stop_all()
            self._output("Global STOP signal placed. All lanes stopped.")
            return
        work_item = args[0]
        lane = self.supervisor.get_lane(work_item)
        if not lane:
            self._output(f"Error: Lane '{work_item}' not found.")
            return
        lane.stop()
        self._output(f"Lane '{work_item}' stopped.")

    def cmd_step(self, args: List[str]) -> None:
        if not args:
            self._output("Usage: step <work_item>")
            return
        work_item = args[0]
        lane = self.supervisor.get_lane(work_item)
        if not lane:
            self._output(f"Error: Lane '{work_item}' not found.")
            return
        res = self.supervisor.step_lane(lane)
        self._output(f"Step outcome: {res}")

    def cmd_cycle(self, args: List[str]) -> None:
        self._output("Cycling all active lanes...")
        results = self.supervisor.cycle_all()
        for res in results:
            self._output(f"  [{res.get('work_item')}] {res.get('action')} -> {res.get('state')}")
        if not results:
            self._output("  No active lanes to cycle.")
        self._output("")

    def cmd_metrics(self, args: List[str]) -> None:
        self._output(self.supervisor.telemetry.format_report())
        self._output("")

    def cmd_insights(self, args: List[str]) -> None:
        self._output(self.insights.format_insights())
        self._output("")

    def cmd_help(self, args: List[str]) -> None:
        lines = [
            "Available Commands:",
            "  status              Show system health and overview",
            "  lanes               List all workflow lanes",
            "  show <work_item>    Show details of a specific lane",
            "  work <objective>    Register a new work objective and wake PM",
            "  step <work_item>    Execute one step on a specific lane",
            "  cycle               Cycle all active lanes once",
            "  pause <work_item>   Pause a lane",
            "  resume <work_item>  Resume a paused lane",
            "  stop [work_item]    Stop a lane or place global STOP",
            "  metrics             Show workflow speed & efficiency metrics",
            "  insights            Show self-improvement bottleneck insights",
            "  help                Show this help message",
            "  quit / exit         Exit the operator console",
        ]
        self._output("\n".join(lines))
        self._output("")

    def run(self) -> None:
        self.print_banner()
        while True:
            try:
                raw = self._input("Orbit> ").strip()
            except (EOFError, KeyboardInterrupt):
                self._output("\nExiting Orbit console.")
                break

            if not raw:
                continue

            parts = shlex.split(raw)
            cmd = parts[0].lower()
            args = parts[1:]

            if cmd in ("quit", "exit", "q"):
                self._output("Exiting Orbit console.")
                break
            elif cmd == "status":
                self.cmd_status(args)
            elif cmd in ("lanes", "list"):
                self.cmd_lanes(args)
            elif cmd == "show":
                self.cmd_show(args)
            elif cmd in ("work", "task", "objective"):
                self.cmd_work(args)
            elif cmd == "pause":
                self.cmd_pause(args)
            elif cmd == "resume":
                self.cmd_resume(args)
            elif cmd == "stop":
                self.cmd_stop(args)
            elif cmd == "step":
                self.cmd_step(args)
            elif cmd == "cycle":
                self.cmd_cycle(args)
            elif cmd == "metrics":
                self.cmd_metrics(args)
            elif cmd == "insights":
                self.cmd_insights(args)
            elif cmd in ("help", "?"):
                self.cmd_help(args)
            else:
                self._output(f"Unknown command: '{cmd}'. Type 'help' for available commands.")
