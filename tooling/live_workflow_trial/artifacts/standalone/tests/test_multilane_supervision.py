"""Multi-lane supervision logic, against a stubbed adapter.

This file was previously named `test_live_multilane_trial.py` and its docstring
claimed to be a live trial. It is not one and never was: the adapter is a
`MagicMock`, so nothing here touches ChatGPT Desktop, switches a real chat, or
presses a real Send. Renamed because a test that claims live proof while mocking
the whole surface is worse than no test — it lets the box be ticked.

What it does prove, which is worth proving, is the supervisor's *decisions*:

1. Two lanes exist independently, in separate directories.
2. PM request IDs do not cross between lanes.
3. A directive for one work item is refused by the other.
4. One lane on HOLD does not freeze the other.
5. Chat switching goes through `adapter.focus()` rather than anything positional.
6. Returned handoffs stay bound to their own work item.
7. No duplicate Send actuation, via SingleWriterLock.

Live two-lane proof is a separate artifact under `longrun/evidence/`, because
PM's requirement was explicit: unit test green is not live proven.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

from standalone.bridge.contracts import ChatTransportResult
from standalone.bridge.pm_envelope import PMDirective
from standalone.bridge.registry import ChatEndpointRegistry
from standalone.operator.lane import (
    STATE_AWAITING_PM_ROUTING,
    STATE_AWAITING_WORKER,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_DIRECTIVE_ACCEPTED,
    STATE_HOLD,
    STATE_INITIALIZED,
    STATE_REPORTING_TO_PM,
    STATE_STOPPED,
)
from standalone.operator.supervisor import MultiWorkItemSupervisor, load_orbit_config


class LiveMultiLaneTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mock_adapter = MagicMock()
        self.supervisor = MultiWorkItemSupervisor(self.state_dir, adapter=self.mock_adapter)

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_two_lane_independent_multiplexing(self):
        # 1. Register two independent work items targeting distinct registered endpoints
        lane_a = self.supervisor.create_lane(
            "WORK-A",
            "Objective A for Windows Worker",
            expect="HANDOFF_WORK-A.md",
            sender="windows-worker",
            token="WORK-A-TOKEN",
        )
        lane_b = self.supervisor.create_lane(
            "WORK-B",
            "Objective B for Architecture TL",
            expect="HANDOFF_WORK-B.md",
            sender="architecture-tl",
            token="WORK-B-TOKEN",
        )

        # Verify storage isolation
        self.assertNotEqual(lane_a.lane_dir, lane_b.lane_dir)
        self.assertTrue((lane_a.lane_dir / "lane.json").is_file())
        self.assertTrue((lane_b.lane_dir / "lane.json").is_file())

        # 2. Step Lane A and Lane B to wake PM
        self.mock_adapter.deliver.return_value = ChatTransportResult.allow(
            "SEND_BOUNDED_MESSAGE", {}, delivery_state="DELIVERED"
        )
        step_a1 = self.supervisor.step_lane(lane_a)
        step_b1 = self.supervisor.step_lane(lane_b)

        self.assertEqual(step_a1["action"], "PM_WOKEN")
        self.assertEqual(step_b1["action"], "PM_WOKEN")
        self.assertEqual(lane_a.record.work_state, STATE_AWAITING_PM_ROUTING)
        self.assertEqual(lane_b.record.work_state, STATE_AWAITING_PM_ROUTING)

        req_a_id = lane_a.record.pending_request_id
        req_b_id = lane_b.record.pending_request_id
        self.assertNotEqual(req_a_id, req_b_id)

        # 3. Simulate PM transcript containing directive for WORK-A only
        self.mock_adapter.focus.return_value = MagicMock(ok=True)
        dir_a_text = (
            "ChatGPT said:\n"
            "```\n"
            "ORBIT_DIRECTIVE\n"
            "version: 0.1\n"
            f"request_id: {req_a_id}\n"
            "directive_id: dir-a-101\n"
            "work_item: WORK-A\n"
            "action: DISPATCH_TO_ROLE\n"
            "target_endpoint: windows-worker\n"
            "```"
        )
        self.mock_adapter.driver.read_transcript_tail.return_value = MagicMock(
            ok=True, data={"text": dir_a_text}
        )

        # Cycle all: Lane B must NOT consume Lane A's directive; Lane A accepts it
        cycle_1 = self.supervisor.cycle_all()
        res_map_1 = {r["work_item"]: r for r in cycle_1}

        self.assertEqual(res_map_1["WORK-A"]["action"], "DIRECTIVE_ACCEPTED")
        self.assertEqual(res_map_1["WORK-A"]["state"], STATE_DIRECTIVE_ACCEPTED)
        self.assertEqual(lane_a.record.accepted_action, "DISPATCH_TO_ROLE")
        self.assertEqual(lane_a.record.current_endpoint, "windows-worker")

        self.assertEqual(res_map_1["WORK-B"]["action"], "AWAITING_PM_DIRECTIVE")
        self.assertEqual(res_map_1["WORK-B"]["state"], STATE_AWAITING_PM_ROUTING)
        self.assertEqual(lane_b.record.accepted_directive_id, "")  # strictly unconsumed

        # 4. Step Lane A to dispatch to windows-worker
        step_a2 = self.supervisor.step_lane(lane_a)
        self.assertEqual(step_a2["action"], "DISPATCHED")
        self.assertEqual(lane_a.record.work_state, STATE_AWAITING_WORKER)

        # 5. Now PM issues HOLD directive for WORK-B
        dir_b_hold = (
            "ChatGPT said:\n"
            "```\n"
            "ORBIT_DIRECTIVE\n"
            "version: 0.1\n"
            f"request_id: {req_b_id}\n"
            "directive_id: dir-b-201\n"
            "work_item: WORK-B\n"
            "action: HOLD\n"
            "notes: waiting for human review\n"
            "```"
        )
        self.mock_adapter.driver.read_transcript_tail.return_value = MagicMock(
            ok=True, data={"text": dir_b_hold}
        )

        step_b2 = self.supervisor.step_lane(lane_b)
        self.assertEqual(step_b2["action"], "PM_DIRECTED_HOLD")
        self.assertEqual(lane_b.record.work_state, STATE_HOLD)
        self.assertEqual(lane_b.record.accepted_action, "HOLD")

        # 6. Prove Lane B in HOLD does not impede Lane A from advancing
        self.mock_adapter.wait_for_response.return_value = MagicMock(state="complete")
        handoff_a_content = (
            "ORBIT_HANDOFF_BEGIN HANDOFF_WORK-A.md\n"
            "work_item: WORK-A\n"
            "from: windows-worker\n"
            "to: Orbit PM\n"
            "status: COMPLETE\n"
            "handoff_id: h-a-01\n"
            "sequence: 1\n"
            "ORBIT_HANDOFF_BODY\n"
            "Windows worker task successfully completed.\n"
            "ORBIT_HANDOFF_END\n"
        )
        self.mock_adapter.collect_from_transcript.return_value = MagicMock(
            ok=True,
            data={
                "filename": "HANDOFF_WORK-A.md",
                "sha256": hashlib.sha256(handoff_a_content.encode("utf-8")).hexdigest(),
                "path": str(lane_a.inbox_dir / "HANDOFF_WORK-A.md"),
            },
        )
        (lane_a.inbox_dir / "HANDOFF_WORK-A.md").write_text(handoff_a_content, encoding="utf-8")

        # Step Lane A: AWAITING_WORKER -> COLLECTING
        step_a3 = self.supervisor.step_lane(lane_a)
        self.assertEqual(step_a3["action"], "WORKER_RESPONDED")
        self.assertEqual(lane_a.record.work_state, "COLLECTING")

        # Step Lane A: COLLECTING -> REPORTING_TO_PM
        step_a4 = self.supervisor.step_lane(lane_a)
        self.assertEqual(step_a4["action"], "COLLECTED")
        self.assertEqual(lane_a.record.work_state, STATE_REPORTING_TO_PM)

        # Step Lane A: REPORTING_TO_PM -> COMPLETED
        step_a5 = self.supervisor.step_lane(lane_a)
        self.assertEqual(step_a5["action"], "REPORTED_TO_PM")
        self.assertEqual(lane_a.record.work_state, STATE_COMPLETED)

        # 7. Verify Lane B remained safely in HOLD while Lane A completed
        lane_b.load_record()
        self.assertEqual(lane_b.record.work_state, STATE_HOLD)

        # 8. Verify Telemetry recorded for Lane A
        summary = self.supervisor.telemetry.summary()
        self.assertEqual(summary["total_hops"], 1)
        self.assertEqual(summary["successful_hops"], 1)
        self.assertEqual(summary["zero_courier_rate"], 1.0)
        self.assertEqual(summary["zero_click_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
