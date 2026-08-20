"""Central operator CLI entrypoint for Orbit.

Provides the one-command launch surface:
    orbit
    orbit status
    orbit work <objective>
    orbit overnight
    orbit lanes
    orbit stop [work_item]
    orbit pause <work_item>
    orbit resume <work_item>
    orbit metrics
    orbit insights
    orbit doctor
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..bridge.apprentice_cli import (
    cmd_await,
    cmd_clear,
    cmd_collect,
    cmd_dispatch,
    cmd_hop,
    cmd_poll,
    cmd_wake,
)
from ..bridge.diagnostics import run as run_diagnostics
from .insights import WorkflowInsightsAnalyzer
from .overnight import OvernightRunner
from .repl import OperatorRepl
from .supervisor import MultiWorkItemSupervisor


def default_state_dir() -> Path:
    """Determine a durable default local state root."""
    if "ORBIT_STATE_DIR" in os.environ:
        return Path(os.environ["ORBIT_STATE_DIR"])
    if sys.platform == "win32" and "LOCALAPPDATA" in os.environ:
        return Path(os.environ["LOCALAPPDATA"]) / "Orbit" / "state"
    return Path.home() / ".orbit" / "state"


def emit_json(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload.get("ok", True) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orbit",
        description="Orbit Autonomous Workflow Operator & Supervisor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  orbit                             # Start interactive console REPL
  orbit status                      # Check health, readiness, and active lanes
  orbit work "Improve worker loop"  # Register objective and request PM routing
  orbit overnight                   # Start unattended multi-lane supervisor
  orbit lanes                       # List all workflow lanes
  orbit metrics                     # View workflow speed & efficiency telemetry
  orbit insights                    # View self-improvement bottleneck proposals
  orbit doctor                      # Check ChatGPT accessibility and prerequisites
""",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Durable state directory (default: %%LOCALAPPDATA%%/Orbit/state)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit output as machine-readable JSON",
    )

    subparsers = parser.add_subparsers(dest="command")

    # Status
    subparsers.add_parser("status", help="Show system health, readiness, and lane summary")

    # Work
    work_parser = subparsers.add_parser("work", help="Register a new objective and wake PM")
    work_parser.add_argument("objective", nargs="+", help="Natural language objective")
    work_parser.add_argument("--work-item", default="", help="Optional specific work item ID")
    work_parser.add_argument("--assignment", default="", help="Optional assignment file path")
    work_parser.add_argument("--artifact", default="", help="Optional artifact attachment path")
    work_parser.add_argument("--expect", default="", help="Optional expected return filename")
    work_parser.add_argument("--sender", default="", help="Optional expected return sender")

    # Overnight
    overnight_parser = subparsers.add_parser("overnight", help="Run unattended overnight supervisor")
    overnight_parser.add_argument("--poll-interval", type=float, default=15.0, help="Polling interval in seconds")
    overnight_parser.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (testing)")

    # Lanes
    subparsers.add_parser("lanes", help="List all workflow lanes")

    # Run / REPL
    subparsers.add_parser("run", help="Start interactive operator console")
    subparsers.add_parser("repl", help="Start interactive operator console")

    # Pause / Resume / Stop
    pause_parser = subparsers.add_parser("pause", help="Pause a specific work item lane")
    pause_parser.add_argument("work_item", help="Work item ID to pause")

    resume_parser = subparsers.add_parser("resume", help="Resume a paused work item lane")
    resume_parser.add_argument("work_item", help="Work item ID to resume")

    stop_parser = subparsers.add_parser("stop", help="Stop a lane or place global STOP")
    stop_parser.add_argument("work_item", nargs="?", default="", help="Optional specific work item ID")

    # Cycle
    subparsers.add_parser("cycle", help="Cycle all active lanes once")

    # Metrics & Insights
    subparsers.add_parser("metrics", help="Display workflow speed & efficiency telemetry")
    subparsers.add_parser("insights", help="Display workflow self-improvement proposals")

    # Doctor
    subparsers.add_parser("doctor", help="Check ChatGPT desktop accessibility and prerequisites")

    args = parser.parse_args(argv)

    state_path = args.state_dir or default_state_dir()
    state_path.mkdir(parents=True, exist_ok=True)

    supervisor = MultiWorkItemSupervisor(state_path)

    cmd = args.command

    # If no command is provided, launch interactive REPL
    if cmd is None or cmd in ("run", "repl"):
        repl = OperatorRepl(supervisor)
        repl.run()
        return 0

    if cmd == "status":
        summary = supervisor.status_summary()
        if args.json:
            return emit_json({"ok": True, **summary})
        surface = summary.get("surface", {})
        print("=== Orbit System Status ===")
        print(f"State Root         : {state_path}")
        print(f"Surface Status     : {surface.get('status', 'UNKNOWN')} (ok={surface.get('ok')}, drivable={surface.get('drivable')})")
        if surface.get("remedy"):
            print(f"Remedy             : {surface.get('remedy')}")
        print(f"Global STOP Active : {summary.get('stopped')}")
        print(f"Lanes              : Total={summary.get('total_lanes')}, Active={summary.get('active_lanes')}, Blocked={summary.get('blocked_lanes')}, Completed={summary.get('completed_lanes')}")
        lanes = summary.get("lanes", [])
        if lanes:
            print("\nLanes:")
            for l in lanes:
                print(f"  - [{l['work_item']}] {l['work_state']:<20} ep={l['current_endpoint'] or 'none':<16} obj='{l['objective'][:30]}'")
        return 0

    if cmd == "lanes":
        lanes = supervisor.list_lanes()
        if args.json:
            return emit_json({"ok": True, "lanes": [l.summary_dict() for l in lanes]})
        if not lanes:
            print("No active or registered workflow lanes.")
            return 0
        print(f"{'WORK ITEM':<16} {'STATE':<22} {'ENDPOINT':<18} {'OBJECTIVE':<30}")
        print("-" * 90)
        for lane in lanes:
            rec = lane.record
            obj = (rec.objective[:27] + "...") if len(rec.objective) > 30 else rec.objective
            print(f"{rec.work_item:<16} {rec.work_state:<22} {rec.current_endpoint or 'none':<18} {obj:<30}")
        return 0

    if cmd == "work":
        objective = " ".join(args.objective)
        import time as _t
        work_item = args.work_item or f"WORK-{int(_t.time()) % 100000:05d}"
        lane = supervisor.create_lane(
            work_item,
            objective,
            assignment_path=args.assignment,
            artifact_path=args.artifact,
            expect=args.expect,
            sender=args.sender,
        )
        print(f"Orbit: objective registered as '{work_item}'; requesting PM routing.")
        step_res = supervisor.step_lane(lane)
        if args.json:
            return emit_json({"ok": True, "work_item": work_item, "step": step_res})
        print(f"Status: {step_res.get('action')} -> {step_res.get('state')}")
        return 0

    if cmd == "overnight":
        runner = OvernightRunner(
            supervisor,
            poll_interval=args.poll_interval,
            max_cycles=args.max_cycles,
        )
        res = runner.run()
        return 0 if res.get("ok") else 1

    if cmd == "pause":
        lane = supervisor.get_lane(args.work_item)
        if not lane:
            print(f"Error: Lane '{args.work_item}' not found.", file=sys.stderr)
            return 1
        lane.pause()
        print(f"Lane '{args.work_item}' paused.")
        return 0

    if cmd == "resume":
        lane = supervisor.get_lane(args.work_item)
        if not lane:
            print(f"Error: Lane '{args.work_item}' not found.", file=sys.stderr)
            return 1
        lane.resume()
        print(f"Lane '{args.work_item}' resumed.")
        return 0

    if cmd == "stop":
        if args.work_item:
            lane = supervisor.get_lane(args.work_item)
            if not lane:
                print(f"Error: Lane '{args.work_item}' not found.", file=sys.stderr)
                return 1
            lane.stop()
            print(f"Lane '{args.work_item}' stopped.")
        else:
            supervisor.stop_all()
            print("Global STOP signal placed. All lanes stopped.")
        return 0

    if cmd == "cycle":
        results = supervisor.cycle_all()
        if args.json:
            return emit_json({"ok": True, "results": results})
        print("Cycled all active lanes:")
        for res in results:
            print(f"  [{res.get('work_item')}] {res.get('action')} -> {res.get('state')}")
        return 0

    if cmd == "metrics":
        report = supervisor.telemetry.format_report()
        if args.json:
            return emit_json({"ok": True, **supervisor.telemetry.summary()})
        print(report)
        return 0

    if cmd == "insights":
        analyzer = WorkflowInsightsAnalyzer(supervisor.telemetry)
        report = analyzer.format_insights()
        if args.json:
            return emit_json({"ok": True, "insights": [i.to_dict() for i in analyzer.analyze()]})
        print(report)
        return 0

    if cmd == "doctor":
        diag_report = run_diagnostics().to_dict()
        if args.json:
            return emit_json({"ok": True, "diagnostics": diag_report})
        print("=== Orbit Bridge Doctor ===")
        print(f"Verdict        : {diag_report.get('verdict')}")
        print(f"Feasible       : {diag_report.get('feasible')}")
        print(f"Reason Code    : {diag_report.get('reason_code')}")
        print(f"Recommendation : {diag_report.get('recommendation')}")
        return 0

    print(f"Unknown command: '{cmd}'", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
