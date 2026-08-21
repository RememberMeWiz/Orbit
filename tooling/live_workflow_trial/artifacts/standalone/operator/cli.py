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
import shutil
import subprocess
import time
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
from .lane import STATE_BLOCKED
from .stateroot import describe, legacy_state_roots, resolve_state_root
from .supervisor import MultiWorkItemSupervisor


def default_state_dir() -> Path:
    """Determine a durable default local state root.

    Deliberately not under AppData. The Store build of Python virtualizes
    %LOCALAPPDATA% and %APPDATA% into its own package LocalCache, so state
    written there is invisible to any other Python and is destroyed by a Python
    reset -- see `stateroot` for the measurements. ORBIT_STATE_DIR is still
    honoured for compatibility with anything already setting it.
    """
    if "ORBIT_STATE_DIR" in os.environ:
        return Path(os.environ["ORBIT_STATE_DIR"])
    return resolve_state_root().resolved


def cmd_migrate_state(state_path: Path) -> int:
    """Move lanes out of a virtualized legacy root into the real one.

    Copy-then-verify rather than move: the legacy copy is left in place so a
    failed migration cannot lose work, and a lane that already exists at the
    destination is never overwritten -- the destination is authoritative once
    anything has run against it.
    """
    root = resolve_state_root(state_path)
    moved, skipped = [], []
    for legacy in legacy_state_roots():
        if legacy.resolve() == root.resolved:
            continue
        lanes_dir = legacy / "lanes"
        if not lanes_dir.is_dir():
            continue
        for lane_dir in sorted(p for p in lanes_dir.iterdir() if p.is_dir()):
            target = root.resolved / "lanes" / lane_dir.name
            if target.exists():
                skipped.append(lane_dir.name)
                continue
            shutil.copytree(lane_dir, target)
            moved.append(lane_dir.name)

    return emit_json({
        "ok": True,
        "state_root": root.to_dict(),
        "migrated": moved,
        "skipped_already_present": skipped,
        "legacy_roots_left_in_place": [str(r) for r in legacy_state_roots()],
    })


def cmd_supervisor(state_path: Path, args) -> int:
    """Bounded operator control over the supervisor process.

    Four verbs and nothing resembling a shell. `ensure-running` is the one an
    unattended setup calls: start when absent, refuse to add a second instance,
    and restart only through a drain when the running process is stale or is
    executing code the checkout no longer contains.
    """
    from .supervisor_process import (drain_path, read_heartbeat, request_drain,
                                     supervisor_status)

    repo_root = Path(__file__).resolve().parents[2]
    root = resolve_state_root(state_path)
    status = supervisor_status(root.resolved, repo_root)
    action = args.action

    if action == "status":
        payload = {"ok": True, "state_root": root.to_dict(), **status,
                   "heartbeat": read_heartbeat(root.resolved)}
        if getattr(args, "json", False):
            return emit_json(payload)
        print("=== Orbit Supervisor ===")
        print(f"State Root      : {describe(root)}")
        print(f"Health          : {status.get('health')} ({status.get('reason_code')})")
        print(f"Running         : {status.get('running')}")
        print(f"PID             : {status.get('pid')}  created {status.get('process_creation_time') or '-'}")
        age = status.get("heartbeat_age_seconds")
        print(f"Heartbeat age   : {('%.0fs' % age) if age is not None else '-'}")
        print(f"Running code    : {status.get('heartbeat_code_fingerprint') or '-'}")
        print(f"Checkout code   : {status.get('current_code_fingerprint')}")
        print(f"Branch / SHA    : {status.get('branch') or '-'} / {str(status.get('git_sha') or '-')[:12]}")
        print(f"Lanes           : {status.get('lane_count')} total, "
              f"{status.get('active_lane_count')} active, {status.get('blocked_lane_count')} blocked")
        print(f"Last action     : {status.get('last_meaningful_action') or '-'}")
        return 0

    if action == "drain":
        return emit_json({**request_drain(root.resolved), "previous_health": status.get("health")})

    if action in ("ensure-running", "restart-safe"):
        healthy = status.get("running") and status.get("health") in ("READY", "STARTING",
                                                                    "WAITING_FOR_SURFACE")
        if healthy and action == "ensure-running":
            return emit_json({"ok": True, "action": "already-running", **status})

        if status.get("running"):
            # Never two supervisors on one state root, and never a hard kill
            # while one might be mid-delivery: ask it to finish and exit.
            request_drain(root.resolved,
                          reason=f"{action}:{status.get('reason_code', '')}")
            for _ in range(60):
                time.sleep(1.0)
                if not supervisor_status(root.resolved, repo_root).get("running"):
                    break
            else:
                return emit_json({"ok": False, "action": "drain-timeout",
                                  "reason_code": "supervisor-did-not-exit", **status})

        started = _spawn_supervisor(repo_root, root.resolved, args)
        return emit_json({"ok": started.get("ok", False), "action": "started", **started})

    return emit_json({"ok": False, "reason_code": f"unknown-action:{action}"})


def _spawn_supervisor(repo_root: Path, state_root: Path, args) -> Dict[str, Any]:
    """Start exactly one supervisor, optionally in its own console window.

    A visible window is worth having: an unattended operator that logs only to
    a file is something you have to remember to go and read, and the point of
    this process is that nobody is watching it.
    """
    from .supervisor_process import clear_drain

    clear_drain(state_root)
    cmd = [sys.executable, "-m", "standalone.operator.cli",
           "--state-dir", str(state_root), "overnight",
           "--poll-interval", str(getattr(args, "interval", 15.0))]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    env["ORBIT_STATE_ROOT"] = str(state_root)

    creation = 0
    if sys.platform == "win32" and getattr(args, "window", False):
        creation = subprocess.CREATE_NEW_CONSOLE
    elif sys.platform == "win32":
        creation = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(cmd, cwd=str(repo_root), env=env,
                                creationflags=creation)
    except OSError as exc:
        return {"ok": False, "reason_code": f"spawn-failed:{exc}"}

    # Give it long enough to write its first heartbeat, so "started" means
    # started rather than "a process id was returned".
    from .supervisor_process import supervisor_status
    for _ in range(30):
        time.sleep(1.0)
        state = supervisor_status(state_root, repo_root)
        if state.get("running"):
            return {"ok": True, "pid": proc.pid, "windowed": bool(getattr(args, "window", False)),
                    **state}
    return {"ok": False, "pid": proc.pid, "reason_code": "no-heartbeat-after-start"}


def supervisor_heartbeat_observed(state_path: Path) -> bool:
    """Whether a live supervisor is actually watching this state root.

    Registering a lane means nothing if nothing will ever step it, so the
    result of `work` says so rather than leaving the operator to assume.
    """
    from .supervisor_process import supervisor_status
    repo_root = Path(__file__).resolve().parents[2]
    return bool(supervisor_status(resolve_state_root(state_path).resolved,
                                  repo_root).get("running"))


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
    sup_parser = subparsers.add_parser("supervisor", help="Manage the always-on supervisor process")
    sup_parser.add_argument("action",
                            choices=("status", "ensure-running", "drain", "restart-safe"),
                            help="status | ensure-running | drain | restart-safe")
    sup_parser.add_argument("--window", action="store_true",
                            help="run in its own console window so it can be watched")
    sup_parser.add_argument("--interval", type=float, default=15.0)

    subparsers.add_parser("migrate-state",
                          help="Move lanes from a virtualized legacy state root into the real one")
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

    if cmd == "supervisor":
        return cmd_supervisor(state_path, args)

    if cmd == "migrate-state":
        return cmd_migrate_state(state_path)

    if cmd == "status":
        summary = supervisor.status_summary()
        if args.json:
            return emit_json({"ok": True, **summary})
        surface = summary.get("surface", {})
        print("=== Orbit System Status ===")
        # The *resolved* path, and loudly if it is not where it was asked for.
        # A shadow location that reads back as the requested one is exactly how
        # a supervisor and a CLI end up unable to see each other's lanes.
        root = resolve_state_root(state_path)
        print(f"State Root         : {describe(root)}")
        legacy = [r for r in legacy_state_roots() if r.resolve() != root.resolved]
        if legacy:
            print(f"Legacy State Found : {', '.join(str(r) for r in legacy)}")
            print(f"                     run 'orbit migrate-state' to move it here")
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

        # Refuse to reuse an identity. Two lanes under one work item share an
        # inbox and a handoff filename, which the collector then refuses as
        # ambiguous -- much later, and looking like a worker's fault.
        supervisor.refresh_lanes()
        if supervisor.get_lane(work_item) is not None:
            return emit_json({
                "ok": False, "result": "LANE_CREATION_FAILED",
                "work_item": work_item, "lane_created": False,
                "reason_code": "work-item-already-exists",
            })

        try:
            lane = supervisor.create_lane(
                work_item,
                objective,
                assignment_path=args.assignment,
                artifact_path=args.artifact,
                expect=args.expect,
                sender=args.sender,
            )
        except Exception as exc:
            return emit_json({
                "ok": False, "result": "LANE_CREATION_FAILED",
                "work_item": work_item, "lane_created": False,
                "reason_code": f"{type(exc).__name__}: {exc}",
            })

        step_res = supervisor.step_lane(lane)
        lane.load_record()
        action = step_res.get("action", "")

        # What actually happened, rather than "the process exited 0".
        #
        # This printed the step action and returned 0 whatever it was, so an
        # arms-only script recorded WAKE_FAILED, WAITING_FOR_TURN and PM_WOKEN
        # as equally successful. Registration is not the same as having asked
        # PM anything, and the difference is the whole point of the command.
        pm_posted = bool(lane.record.pending_request_id) and action == "PM_WOKEN"
        if action == "PM_WOKEN":
            result, ok = "LANE_REGISTERED_AND_PM_WOKEN", True
        elif action == "WAITING_FOR_TURN":
            result, ok = "LANE_REGISTERED_PENDING_TURN", True
        elif lane.record.work_state == STATE_BLOCKED:
            result, ok = "LANE_REGISTERED_BUT_BLOCKED", False
        else:
            result, ok = "LANE_REGISTERED_PENDING_TURN", True

        payload = {
            "ok": ok,
            "result": result,
            "work_item": work_item,
            "lane_created": True,
            "work_state": lane.record.work_state,
            "step_action": action,
            "pending_request_id": lane.record.pending_request_id,
            "pm_request_posted": pm_posted,
            "reason_code": step_res.get("reason_code", "") or lane.record.blocker_code,
            "supervisor_observed": supervisor_heartbeat_observed(state_path),
            "expected_handoff": lane.record.expected_handoff,
            "state_root": str(resolve_state_root(state_path).resolved),
        }
        if args.json:
            return emit_json(payload)

        print(f"Orbit: objective registered as '{work_item}'.")
        print(f"Result             : {result}")
        print(f"PM request posted  : {pm_posted}"
              + (f" ({lane.record.pending_request_id})" if pm_posted else ""))
        print(f"Lane state         : {lane.record.work_state}")
        if payload["reason_code"]:
            print(f"Reason             : {payload['reason_code']}")
        if not payload["supervisor_observed"]:
            print("Supervisor         : NOT RUNNING — this lane will not advance "
                  "until 'orbit supervisor ensure-running'")
        return 0 if ok else 1

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
