"""Operator entry point for the PM-supervised apprenticeship loop.

Deliberately step-at-a-time rather than one long autonomous run. Each verb is a
separate invocation that leaves durable state behind, so the loop can be driven,
inspected, interrupted and resumed — and so a crash between any two steps is an
ordinary restart rather than a special case.

    status     what Orbit is currently waiting on
    wake       post an ORBIT_PM_REQUEST into the PM conversation
    poll       look for a PM directive answering the pending request
    dispatch   carry out the accepted directive
    await      wait for the target conversation to finish responding
    collect    materialise and validate the expected artifact
    cycle      all of the above as one governed round trip
    clear      remove staged attachments after an abandoned dispatch

Nothing here decides routing. `dispatch` acts only on a directive PM already
issued, and refuses if that directive names an endpoint which is not registered.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from .accessibility import AccessibilityGuard
from .chatgpt import ChatGptAdapter
from .delivery import DeliveryLedger
from .orchestrator import ApprenticeLoop
from .pm_envelope import PMBridgeState, PMDirective
from .registry import ChatEndpointRegistry
from .roundtrip import RoundTrip
from .teaching import TeachingTraceStore

CONFIG = Path(__file__).with_name("orbit_endpoints.json")


def load_config() -> Dict[str, Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def build_loop(state_dir: Path, work_item: str, *, timeout: float = 300.0) -> ApprenticeLoop:
    config = load_config()
    registry = ChatEndpointRegistry.from_orbit_config()
    adapter = ChatGptAdapter(
        registry,
        project_scope=config["project_scope"],
        workflow_scope=config["workflow_scope"],
        chat_list_name=config["chat_list_name"],
    )
    adapter.driver.timeout = timeout

    state_dir.mkdir(parents=True, exist_ok=True)
    return ApprenticeLoop(
        adapter=adapter,
        pm_state=PMBridgeState(state_dir / "pm_bridge.json", work_item=work_item),
        ledger=DeliveryLedger(state_dir / "delivery.json", work_item=work_item),
        traces=TeachingTraceStore(state_dir / "teaching_traces.jsonl", work_item=work_item),
        work_item=work_item,
        inbox_dir=state_dir / "inbox",
        stop_path=state_dir / "STOP",
    )


def preflight(loop: ApprenticeLoop, args) -> Dict[str, Any]:
    """Confirm the app surface is usable before any verb that touches it.

    Returned as a payload rather than raised, so a blocked surface is reported
    with its remedy instead of surfacing as a driver error further in.
    """
    outcome = AccessibilityGuard(loop.adapter.driver,
                                 chat_list_name=loop.adapter.chat_list_name
                                 ).ensure(allow_launch=not args.no_launch)
    # `drivable`, not `ok`: every verb focuses the endpoint it needs, so a
    # conversation stuck behind a prompt must not block reaching a different one.
    if outcome.drivable:
        return {}
    return {"ok": False, "action": "SURFACE_UNAVAILABLE", **outcome.to_dict()}


def emit(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0 if payload.get("ok", True) else 1


def cmd_status(loop: ApprenticeLoop, args) -> int:
    pm = loop.pm_state.load()
    pending = pm.get("pending_request")
    guard = AccessibilityGuard(loop.adapter.driver,
                               chat_list_name=loop.adapter.chat_list_name).observe()
    ready = loop.adapter.surface_ready()
    return emit({
        "ok": True,
        "work_item": loop.work_item,
        "stopped": loop.stopped(),
        "surface_ready": ready.ok,
        "surface_reason": ready.reason_code,
        "accessibility": guard.to_dict(),
        "observed_chats": ready.data.get("chat_items", []),
        "pending_request": pending,
        "consumed_directives": pm.get("consumed_directive_ids", []),
        "open_deliveries": loop.ledger.open_records(),
        "traces": len(loop.traces.all()),
    })


def cmd_wake(loop: ApprenticeLoop, args) -> int:
    blocked = preflight(loop, args)
    if blocked:
        return emit(blocked)

    out = loop.wake_pm(reason=args.reason, nonce=args.nonce,
                       artifact_id=args.artifact_id, artifact_digest=args.artifact_sha256)
    return emit({"ok": out.action == "PM_WOKEN", **out.to_dict()})


def cmd_poll(loop: ApprenticeLoop, args) -> int:
    blocked = preflight(loop, args)
    if blocked:
        return emit(blocked)

    out = loop.await_directive(timeout=args.timeout)
    return emit({"ok": out.action == "DIRECTIVE_ACCEPTED", **out.to_dict()})


def cmd_dispatch(loop: ApprenticeLoop, args) -> int:
    blocked = preflight(loop, args)
    if blocked:
        return emit(blocked)

    pm = loop.pm_state.load()
    if not pm.get("pending_request"):
        return emit({"ok": False, "action": "NO_PENDING_REQUEST"})

    # The directive must be re-read from the PM conversation itself, never from
    # whichever chat happens to be in front. Focus first, then read.
    focused = loop.adapter.focus(loop.pm_endpoint_id)
    if not focused.ok:
        return emit({"ok": False, "action": "PM_FOCUS_FAILED", "reason_code": focused.reason_code})

    tail = loop.adapter.driver.read_transcript_tail(8000)
    verdict = loop.pm_state.evaluate(str(tail.data.get("text", ""))) if tail.ok else None
    if verdict is None or not verdict.accepted:
        return emit({"ok": False, "action": "NO_ACTIVE_DIRECTIVE",
                     "reason_code": verdict.reason_code if verdict else "transcript-unreadable"})

    directive: PMDirective = verdict.directive
    assignment = Path(args.assignment).read_text(encoding="utf-8")
    artifact = Path(args.artifact) if args.artifact else None

    out = loop.dispatch(directive=directive, assignment=assignment,
                        verify_token=args.token, artifact_path=artifact)
    if out.action == "DISPATCHED":
        loop.record(directive=directive, action=directive.action,
                    state_before={"work_state": "awaiting-dispatch"},
                    state_after={"delivery_state": out.data.get("delivery_state")},
                    evidence={"endpoint": directive.target_endpoint,
                              "artifact": str(artifact) if artifact else ""},
                    result="dispatched", classification="success",
                    reason="pm-directed-dispatch")
        loop.consume(directive)
    return emit({"ok": out.action == "DISPATCHED", "directive": directive.to_dict(), **out.to_dict()})


def cmd_await(loop: ApprenticeLoop, args) -> int:
    blocked = preflight(loop, args)
    if blocked:
        return emit(blocked)

    focused = loop.adapter.focus(args.endpoint)
    if not focused.ok:
        return emit({"ok": False, "action": "FOCUS_FAILED", "reason_code": focused.reason_code})
    out = loop.await_worker(timeout=args.timeout)
    return emit({"ok": out.action == "WORKER_RESPONDED", **out.to_dict()})


def cmd_collect(loop: ApprenticeLoop, args) -> int:
    blocked = preflight(loop, args)
    if blocked:
        return emit(blocked)

    out = loop.collect(endpoint_id=args.endpoint, expected_name=args.expect,
                       expected_sender=args.sender)
    return emit({"ok": out.action == "COLLECTED", **out.to_dict()})


def cmd_cycle(loop: ApprenticeLoop, args) -> int:
    """One full zero-courier round trip, PM decision included."""
    cycle = RoundTrip(
        loop,
        journal_path=Path(args.state_dir) / "roundtrip_journal.jsonl",
        allow_launch=not args.no_launch,
        observer=lambda step, entry: print(
            f"[{entry['at']}] {step}: {entry.get('action') or entry.get('status')}"
            f" {entry.get('reason_code', '')}".rstrip(), flush=True),
    )
    result = cycle.run(
        reason=args.reason,
        nonce=args.nonce,
        assignment=Path(args.assignment).read_text(encoding="utf-8"),
        verify_token=args.token,
        expected_artifact=args.expect,
        artifact_path=Path(args.artifact) if args.artifact else None,
        expected_sender=args.sender,
        pm_timeout=args.pm_timeout,
        worker_timeout=args.worker_timeout,
    )
    return emit({"ok": result.completed, **result.to_dict()})


def cmd_clear(loop: ApprenticeLoop, args) -> int:
    blocked = preflight(loop, args)
    if blocked:
        return emit(blocked)

    result = loop.adapter.clear_attachments()
    return emit({"ok": result.ok, "reason_code": result.reason_code, "data": result.data})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Orbit apprenticeship loop")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--work-item", required=True)
    parser.add_argument("--driver-timeout", type=float, default=300.0)
    parser.add_argument("--no-launch", action="store_true",
                        help="never start ChatGPT Desktop; report a dead surface instead")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    wake = sub.add_parser("wake")
    wake.add_argument("--reason", required=True)
    wake.add_argument("--nonce", required=True)
    wake.add_argument("--artifact-id", default="")
    wake.add_argument("--artifact-sha256", default="")
    wake.set_defaults(func=cmd_wake)

    poll = sub.add_parser("poll")
    poll.add_argument("--timeout", type=float, default=60.0)
    poll.set_defaults(func=cmd_poll)

    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("--assignment", required=True, help="file containing the assignment text")
    dispatch.add_argument("--token", required=True, help="string that must appear in the composer")
    dispatch.add_argument("--artifact", default="")
    dispatch.set_defaults(func=cmd_dispatch)

    waiter = sub.add_parser("await")
    waiter.add_argument("--endpoint", required=True)
    waiter.add_argument("--timeout", type=float, default=900.0)
    waiter.set_defaults(func=cmd_await)

    collect = sub.add_parser("collect")
    collect.add_argument("--endpoint", required=True)
    collect.add_argument("--expect", required=True)
    collect.add_argument("--sender", default="")
    collect.set_defaults(func=cmd_collect)

    cycle = sub.add_parser("cycle", help="run one full PM -> worker -> PM round trip")
    cycle.add_argument("--reason", required=True, help="what Orbit is asking PM to decide")
    cycle.add_argument("--nonce", required=True)
    cycle.add_argument("--assignment", required=True, help="file containing the assignment text")
    cycle.add_argument("--token", required=True, help="string that must appear in the composer")
    cycle.add_argument("--expect", required=True, help="filename the worker must return")
    cycle.add_argument("--artifact", default="", help="file to attach to the assignment")
    cycle.add_argument("--sender", default="")
    cycle.add_argument("--pm-timeout", type=float, default=1800.0)
    cycle.add_argument("--worker-timeout", type=float, default=1800.0)
    cycle.set_defaults(func=cmd_cycle)

    sub.add_parser("clear").set_defaults(func=cmd_clear)

    args = parser.parse_args(argv)
    loop = build_loop(args.state_dir, args.work_item, timeout=args.driver_timeout)
    return args.func(loop, args)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
