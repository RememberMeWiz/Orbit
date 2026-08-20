"""Tests for Orbit MultiWorkItemSupervisor and lane isolation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from standalone.bridge.contracts import ChatTransportResult
from standalone.bridge.pm_envelope import DirectiveVerdict, PMDirective
from standalone.operator.lane import (
    STATE_AWAITING_PM_ROUTING,
    STATE_AWAITING_WORKER,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_DIRECTIVE_ACCEPTED,
    STATE_INITIALIZED,
    STATE_PAUSED,
    STATE_REPORTING_TO_PM,
    STATE_STOPPED,
    WorkItemLane,
)
from standalone.operator.supervisor import MultiWorkItemSupervisor


class TestMultiWorkItemSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mock_adapter = MagicMock()
        self.supervisor = MultiWorkItemSupervisor(self.state_dir, adapter=self.mock_adapter)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_load_lanes(self):
        lane1 = self.supervisor.create_lane("WORK-001", "Objective 1")
        lane2 = self.supervisor.create_lane("WORK-002", "Objective 2")

        self.assertEqual(len(self.supervisor.list_lanes()), 2)
        self.assertEqual(self.supervisor.get_lane("WORK-001").record.objective, "Objective 1")
        self.assertEqual(self.supervisor.get_lane("WORK-002").record.objective, "Objective 2")

        # Simulate fresh process restart by creating a new supervisor instance on the same directory
        restarted_supervisor = MultiWorkItemSupervisor(self.state_dir, adapter=self.mock_adapter)
        self.assertEqual(len(restarted_supervisor.list_lanes()), 2)
        self.assertEqual(restarted_supervisor.get_lane("WORK-001").record.objective, "Objective 1")
        self.assertEqual(restarted_supervisor.get_lane("WORK-002").record.objective, "Objective 2")

    def test_lane_isolation_storage(self):
        lane1 = self.supervisor.create_lane("WORK-001", "Objective 1")
        lane2 = self.supervisor.create_lane("WORK-002", "Objective 2")

        self.assertNotEqual(lane1.lane_dir, lane2.lane_dir)
        self.assertNotEqual(lane1.pm_state_path, lane2.pm_state_path)
        self.assertNotEqual(lane1.delivery_path, lane2.delivery_path)
        self.assertNotEqual(lane1.inbox_dir, lane2.inbox_dir)

    def test_lane_stop_and_pause_independence(self):
        lane1 = self.supervisor.create_lane("WORK-001", "Objective 1")
        lane2 = self.supervisor.create_lane("WORK-002", "Objective 2")

        # Pause lane 1
        lane1.pause()
        self.assertTrue(lane1.paused())
        self.assertFalse(lane2.paused())

        # Resume lane 1
        lane1.resume()
        self.assertFalse(lane1.paused())

        # Stop lane 2
        lane2.stop()
        self.assertTrue(lane2.stopped())
        self.assertFalse(lane1.stopped())

    def test_global_stop_affects_all(self):
        lane1 = self.supervisor.create_lane("WORK-001", "Objective 1")
        lane2 = self.supervisor.create_lane("WORK-002", "Objective 2")

        self.supervisor.stop_all()
        self.assertTrue(self.supervisor.stopped())
        self.assertTrue(lane1.stopped())
        self.assertTrue(lane2.stopped())

    def test_directive_isolation_between_lanes(self):
        lane1 = self.supervisor.create_lane("WORK-001", "Objective 1")
        lane2 = self.supervisor.create_lane("WORK-002", "Objective 2")

        # Step lane 1 to INITIALIZED -> AWAITING_PM_ROUTING
        self.mock_adapter.deliver.return_value = ChatTransportResult.allow("SEND_BOUNDED_MESSAGE", {}, delivery_state="DELIVERED")
        res1 = self.supervisor.step_lane(lane1)
        self.assertEqual(res1["action"], "PM_WOKEN")
        self.assertEqual(lane1.record.work_state, STATE_AWAITING_PM_ROUTING)

        # Step lane 2 to INITIALIZED -> AWAITING_PM_ROUTING
        res2 = self.supervisor.step_lane(lane2)
        self.assertEqual(res2["action"], "PM_WOKEN")
        self.assertEqual(lane2.record.work_state, STATE_AWAITING_PM_ROUTING)

        # A directive arrives for WORK-001 only
        self.mock_adapter.focus.return_value = MagicMock(ok=True)
        directive_text = (
            "ChatGPT said:\n"
            "```\n"
            "ORBIT_DIRECTIVE\n"
            "version: 0.1\n"
            f"request_id: {lane1.record.pending_request_id}\n"
            "directive_id: dir-001\n"
            "work_item: WORK-001\n"
            "action: DISPATCH_TO_ROLE\n"
            "target_endpoint: windows-worker\n"
            "```"
        )
        self.mock_adapter.driver.read_transcript_tail.return_value = MagicMock(ok=True, data={"text": directive_text})

        # Step lane 2 first: lane 2 MUST NOT accept WORK-001's directive
        step_res2 = self.supervisor.step_lane(lane2)
        self.assertEqual(step_res2["action"], "AWAITING_PM_DIRECTIVE")
        self.assertEqual(lane2.record.work_state, STATE_AWAITING_PM_ROUTING)

        # Step lane 1: lane 1 DOES accept WORK-001's directive
        step_res1 = self.supervisor.step_lane(lane1)
        self.assertEqual(step_res1["action"], "DIRECTIVE_ACCEPTED")
        self.assertEqual(lane1.record.work_state, STATE_DIRECTIVE_ACCEPTED)
        self.assertEqual(lane1.record.accepted_directive_id, "dir-001")
        self.assertEqual(lane1.record.current_endpoint, "windows-worker")

    def test_one_lane_blocked_does_not_freeze_other(self):
        lane1 = self.supervisor.create_lane("WORK-001", "Objective 1")
        lane2 = self.supervisor.create_lane("WORK-002", "Objective 2")

        lane1.record.work_state = STATE_BLOCKED
        lane1.record.blocker_code = "syntax-error"
        lane1.save_record()

        lane2.record.work_state = STATE_INITIALIZED
        lane2.save_record()

        self.mock_adapter.deliver.return_value = ChatTransportResult.allow("SEND_BOUNDED_MESSAGE", {}, delivery_state="DELIVERED")

        # Cycling all active lanes
        results = self.supervisor.cycle_all()
        # Lane 1 was blocked (idle), Lane 2 stepped to PM_WOKEN
        self.assertEqual(len(results), 2)
        res_map = {r["work_item"]: r for r in results}
        self.assertEqual(res_map["WORK-001"]["state"], STATE_BLOCKED)
        self.assertEqual(res_map["WORK-002"]["state"], STATE_AWAITING_PM_ROUTING)


if __name__ == "__main__":
    unittest.main()
