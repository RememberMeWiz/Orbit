"""Live two-lane supervision trial against the real ChatGPT Desktop app.

PM required this to be live: unit test green is not live proven. Nothing here is
mocked. The supervisor drives the real adapter, switches real conversations, and
presses real Send buttons.

Evidence is captured as it happens rather than reconstructed afterwards, because
the properties being proven are mostly negative -- ids that did *not* cross,
sends that did *not* duplicate, a human who did *not* touch anything -- and
those cannot be recovered from a final state snapshot.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"E:\Users\Louis\Documents\Orbit\tooling\live_workflow_trial\artifacts")

from standalone.operator.supervisor import MultiWorkItemSupervisor

SCRATCH = Path(r"C:\Users\louis\AppData\Local\Temp\claude\E--Users-Louis-Documents-Orbit\3b6b4c7d-9fd1-4679-95ea-c3b88dfcc1ea\scratchpad")
STATE = SCRATCH / "twolane"
JOURNAL = SCRATCH / "twolane_journal.jsonl"

# A fresh work item per run. Reusing one across runs leaves several handoffs
# with the same filename in the same conversation, which the collector correctly
# refuses as ambiguous -- real work items are unique, so the harness should be
# too rather than the product being loosened to tolerate a test artefact.
RUN = time.strftime("%H%M%S")

LANES = [
    dict(work_item=f"M0-LANE-A-{RUN}", role="windows-worker", sender="WORKER",
         token="ORBIT-LANE-A-TOKEN", template=SCRATCH / "lane_A.md",
         objective="Lane A: route to windows-worker. Concurrent-lane interference analysis."),
    dict(work_item=f"M0-LANE-B-{RUN}", role="architecture-tl", sender="ARCHITECTURE",
         token="ORBIT-LANE-B-TOKEN", template=SCRATCH / "lane_B.md",
         objective="Lane B: route to architecture-tl. Lane fairness and window-scheduling policy."),
]


def note(entry: dict) -> None:
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with JOURNAL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    print(json.dumps(entry, sort_keys=True, default=str), flush=True)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    supervisor = MultiWorkItemSupervisor(STATE)

    surface = supervisor.check_surface()
    note({"event": "preflight", "status": surface.get("status"),
          "drivable": surface.get("drivable")})
    if not surface.get("drivable"):
        note({"event": "aborted", "reason": surface.get("reason_code"),
              "remedy": surface.get("remedy")})
        return 1

    for spec in LANES:
        # Rewrite the template for this run's work item so the handoff the
        # worker is asked for matches the one Orbit will look for.
        base = "M0-LANE-A-001" if spec["sender"] == "WORKER" else "M0-LANE-B-001"
        text = spec["template"].read_text(encoding="utf-8").replace(base, spec["work_item"])
        assignment = SCRATCH / f"assignment_{spec['work_item']}.md"
        assignment.write_text(text, encoding="utf-8")
        spec["assignment"] = assignment

        lane = supervisor.create_lane(
            spec["work_item"], spec["objective"],
            assignment_path=str(spec["assignment"]),
            expect=f"HANDOFF_{spec['work_item']}_{spec['sender']}_TO_ORBIT.md",
            sender=spec["sender"], token=spec["token"], source="transcript",
            nonce=f"{spec['work_item']}-twolane",
        )
        note({"event": "lane_created", "work_item": lane.work_item,
              "expect": lane.record.expected_handoff, "dir": str(lane.lane_dir)})

    # Every Send the adapter makes, counted independently of what the supervisor
    # believes it did. Duplicate actuation is the property most worth measuring.
    sends: list[str] = []
    original_send = supervisor.adapter.send

    def counting_send(*args, **kwargs):
        result = original_send(*args, **kwargs)
        target = kwargs.get("expect_endpoint_id", "")
        sends.append(target)
        note({"event": "send", "endpoint": target, "ok": result.ok,
              "reason": result.reason_code, "total_sends": len(sends)})
        return result

    supervisor.adapter.send = counting_send

    deadline = time.time() + 3600
    round_number = 0
    while time.time() < deadline:
        round_number += 1
        results = supervisor.cycle_all()
        for result in results:
            note({"event": "step", "round": round_number, **result})

        summary = supervisor.status_summary()
        states = {lane["work_item"]: lane["work_state"] for lane in summary["lanes"]}
        terminal = {"COMPLETED", "BLOCKED", "STOPPED", "HOLD"}
        if all(state in terminal for state in states.values()):
            note({"event": "all_lanes_terminal", "states": states})
            break
        time.sleep(5)

    summary = supervisor.status_summary()
    lanes = {lane["work_item"]: lane for lane in summary["lanes"]}

    note({"event": "final", "rounds": round_number, "total_sends": len(sends),
          "sends_by_endpoint": {e: sends.count(e) for e in set(sends)},
          "states": {w: l["work_state"] for w, l in lanes.items()}})

    # The negative properties, checked explicitly rather than assumed.
    request_ids = {w: l.get("pending_request_id", "") for w, l in lanes.items()}
    note({"event": "request_ids", "ids": request_ids,
          "crossed": len(set(v for v in request_ids.values() if v)) < len(
              [v for v in request_ids.values() if v])})

    for work_item, lane in lanes.items():
        inbox = STATE / "lanes" / work_item / "inbox"
        collected = sorted(p.name for p in inbox.glob("*")) if inbox.exists() else []
        note({"event": "collected", "work_item": work_item, "files": collected,
              "all_bound_to_work_item": all(work_item in name for name in collected)})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
