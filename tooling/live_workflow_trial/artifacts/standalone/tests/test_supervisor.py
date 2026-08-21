"""Tests for Orbit MultiWorkItemSupervisor, lane isolation, exact directive semantics, and workflow scope."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from standalone.bridge.contracts import BridgeError, ChatTransportResult
from standalone.bridge.pm_envelope import DirectiveVerdict, PMDirective
from standalone.operator.lane import (
    STATE_AWAITING_PM_ROUTING,
    STATE_AWAITING_WORKER,
    STATE_BLOCKED,
    STATE_COLLECTING,
    STATE_COMPLETED,
    STATE_DIRECTIVE_ACCEPTED,
    STATE_HOLD,
    STATE_INITIALIZED,
    STATE_PAUSED,
    STATE_REPORTING_TO_PM,
    STATE_STOPPED,
    WorkItemLane,
)
from standalone.operator.supervisor import MultiWorkItemSupervisor, load_orbit_config


def _config_without(tmpdir, key):
    """A committed config with one scope key removed."""
    import json, pathlib
    from standalone.operator.supervisor import DEFAULT_CONFIG_PATH
    raw = json.loads(pathlib.Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    raw.pop(key, None)
    target = pathlib.Path(tmpdir) / "endpoints.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


class ContentionTests(unittest.TestCase):
    """Sharing one window must not mean lanes killing each other."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = MagicMock()
        from standalone.operator.supervisor import MultiWorkItemSupervisor
        self.sup = MultiWorkItemSupervisor(Path(self.tmp.name), adapter=self.adapter)

    def tearDown(self):
        self.tmp.cleanup()

    def lane(self, work_item="W-1"):
        return self.sup.create_lane(work_item, "objective", expect=f"HANDOFF_{work_item}_A_TO_B.md",
                                    token="TOK")

    def wake_returns(self, reason):
        from standalone.bridge.contracts import ChatTransportResult
        self.adapter.deliver.return_value = ChatTransportResult.deny(
            "SEND_BOUNDED_MESSAGE", reason, "")

    def test_SUP_CONT_001_a_busy_window_is_not_a_dead_lane(self):
        """Found live: lane B woke PM a second after lane A and was killed."""
        lane = self.lane()
        self.wake_returns("response-in-progress")
        out = self.sup.step_lane(lane)
        self.assertEqual(out["action"], "WAITING_FOR_TURN")
        self.assertNotEqual(lane.record.work_state, STATE_BLOCKED)
        self.assertEqual(lane.record.transient_count, 1)

    def test_SUP_CONT_002_a_busy_ledger_is_not_a_dead_lane_either(self):
        lane = self.lane()
        self.wake_returns("writer-busy")
        self.assertEqual(self.sup.step_lane(lane)["action"], "WAITING_FOR_TURN")

    def test_SUP_CONT_003_a_real_failure_still_blocks_immediately(self):
        lane = self.lane()
        self.wake_returns("endpoint-not-registered")
        out = self.sup.step_lane(lane)
        self.assertEqual(out["action"], "WAKE_FAILED")
        self.assertEqual(lane.record.work_state, STATE_BLOCKED)

    def test_SUP_CONT_004_endless_contention_eventually_blocks(self):
        """Retrying forever would hide a genuine stall."""
        from standalone.operator.supervisor import MAX_CONSECUTIVE_TRANSIENT
        lane = self.lane()
        self.wake_returns("response-in-progress")
        for _ in range(MAX_CONSECUTIVE_TRANSIENT):
            out = self.sup.step_lane(lane)
        self.assertEqual(lane.record.work_state, STATE_BLOCKED)
        self.assertIn("consecutive attempts", lane.record.blocker_detail)

    def test_SUP_CONT_005_progress_clears_the_contention_count(self):
        """It counts contention, not lifetime."""
        from standalone.bridge.contracts import ChatTransportResult
        lane = self.lane()
        self.wake_returns("response-in-progress")
        self.sup.step_lane(lane)
        self.sup.step_lane(lane)
        self.assertEqual(lane.record.transient_count, 2)

        self.adapter.deliver.return_value = ChatTransportResult.allow(
            "SEND_BOUNDED_MESSAGE", {"request_id": "r1"}, delivery_state="SENT_UNCONFIRMED")
        self.sup.step_lane(lane)
        self.assertEqual(lane.record.transient_count, 0)

    def test_SUP_CONT_006_contention_state_survives_a_restart(self):
        lane = self.lane()
        self.wake_returns("response-in-progress")
        self.sup.step_lane(lane)
        from standalone.operator.supervisor import MultiWorkItemSupervisor
        resumed = MultiWorkItemSupervisor(Path(self.tmp.name), adapter=self.adapter)
        again = resumed.get_lane("W-1")
        again.load_record()
        self.assertEqual(again.record.transient_count, 1)


class WorkerWaitTests(unittest.TestCase):
    """Waiting must survive missing the moment the worker replied."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.adapter = MagicMock()
        from standalone.operator.supervisor import MultiWorkItemSupervisor
        self.sup = MultiWorkItemSupervisor(Path(self.tmp.name), adapter=self.adapter)
        self.lane = self.sup.create_lane("W-1", "objective",
                                         expect="HANDOFF_W-1_A_TO_B.md", token="TOK")
        self.lane.record.work_state = STATE_AWAITING_WORKER
        self.lane.record.current_endpoint = "windows-worker"
        self.lane.save_record()
        self.adapter.focus.return_value = MagicMock(ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def observe(self, state):
        self.adapter.driver.response_state.return_value = MagicMock(
            ok=True, data={"state": state}, reason_code="ok")

    def test_SUP_WAIT_001_an_idle_window_is_checked_for_the_handoff(self):
        """Streaming may have finished entirely between two samples."""
        self.observe("idle")
        out = self.sup.step_lane(self.lane)
        self.assertEqual(out["action"], "WORKER_IDLE_TRY_COLLECT")
        self.assertEqual(self.lane.record.work_state, STATE_COLLECTING)

    def test_SUP_WAIT_002_completion_never_depends_on_witnessing_streaming(self):
        """The defect: saw_streaming stayed false and the lane waited forever."""
        self.observe("idle")
        self.sup.step_lane(self.lane)
        self.assertFalse(self.lane.record.saw_streaming)
        self.assertEqual(self.lane.record.work_state, STATE_COLLECTING)

    def test_SUP_WAIT_003_a_streaming_window_is_left_alone(self):
        self.observe("streaming")
        out = self.sup.step_lane(self.lane)
        self.assertEqual(out["action"], "AWAITING_WORKER_RESPONSE")
        self.assertEqual(self.lane.record.work_state, STATE_AWAITING_WORKER)

    def test_SUP_WAIT_004_no_handoff_yet_returns_to_waiting(self):
        """Not finished writing is not a failure."""
        from standalone.bridge.contracts import ChatTransportResult
        self.lane.record.work_state = STATE_COLLECTING
        self.lane.save_record()
        self.adapter.collect_from_transcript.return_value = ChatTransportResult.deny(
            "COLLECT_EXPECTED_ARTIFACT", "transcript-handoff-not-found", "")
        out = self.sup.step_lane(self.lane)
        self.assertEqual(out["action"], "AWAITING_WORKER_RESPONSE")
        self.assertEqual(self.lane.record.work_state, STATE_AWAITING_WORKER)
        self.assertNotEqual(self.lane.record.work_state, STATE_BLOCKED)

    def test_SUP_WAIT_005_an_unreadable_window_is_reported_not_guessed(self):
        self.adapter.driver.response_state.return_value = MagicMock(
            ok=False, reason_code="chat-window-unavailable", data={})
        out = self.sup.step_lane(self.lane)
        self.assertEqual(out["action"], "WORKER_STATE_UNREADABLE")


class LaneDiscoveryTests(unittest.TestCase):
    """A supervisor that cannot see new work is not autonomous."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.adapter = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()

    def supervisor(self):
        from standalone.operator.supervisor import MultiWorkItemSupervisor
        return MultiWorkItemSupervisor(self.root, adapter=self.adapter)

    def names(self, sup):
        return sorted(l.work_item for l in sup.list_lanes())

    def test_SUP_DISC_001_a_lane_created_after_startup_is_discovered(self):
        """The reported root cause: lanes registered by another process."""
        running = self.supervisor()
        self.assertEqual(self.names(running), [])

        other_process = self.supervisor()
        other_process.create_lane("W-A", "objective A", expect="HANDOFF_W-A_X_TO_ORBIT.md")

        running.refresh_lanes()
        self.assertEqual(self.names(running), ["W-A"])

    def test_SUP_DISC_002_an_existing_lane_keeps_its_state_when_another_appears(self):
        running = self.supervisor()
        lane = running.create_lane("W-A", "objective A", expect="HANDOFF_W-A_X_TO_ORBIT.md")
        lane.record.work_state = STATE_AWAITING_WORKER
        lane.record.pending_request_id = "pmreq-keepme"
        lane.save_record()

        self.supervisor().create_lane("W-B", "objective B", expect="HANDOFF_W-B_X_TO_ORBIT.md")
        running.refresh_lanes()

        self.assertEqual(self.names(running), ["W-A", "W-B"])
        kept = running.get_lane("W-A")
        self.assertEqual(kept.record.work_state, STATE_AWAITING_WORKER)
        self.assertEqual(kept.record.pending_request_id, "pmreq-keepme")

    def test_SUP_DISC_003_a_malformed_lane_blocks_without_stopping_the_others(self):
        running = self.supervisor()
        running.create_lane("W-A", "objective A", expect="HANDOFF_W-A_X_TO_ORBIT.md")
        bad = self.root / "lanes" / "W-BAD"
        bad.mkdir(parents=True)
        (bad / "lane.json").write_text("{ this is not json", encoding="utf-8")

        running.refresh_lanes()
        self.assertIn("W-BAD", running.malformed_lanes)
        self.assertIn("W-A", self.names(running))

    def test_SUP_DISC_004_a_malformed_lane_is_never_reinitialised(self):
        """Rewriting a state file we failed to parse restarts in-flight work."""
        running = self.supervisor()
        bad = self.root / "lanes" / "W-BAD"
        bad.mkdir(parents=True)
        original = "{ this is not json"
        (bad / "lane.json").write_text(original, encoding="utf-8")

        running.refresh_lanes()
        self.assertEqual((bad / "lane.json").read_text(encoding="utf-8"), original)

    def test_SUP_DISC_005_identity_mismatch_is_refused(self):
        running = self.supervisor()
        lane = running.create_lane("W-A", "objective", expect="HANDOFF_W-A_X_TO_ORBIT.md")
        record = json.loads((lane.lane_dir / "lane.json").read_text(encoding="utf-8"))
        record["work_item"] = "W-SOMETHING-ELSE"
        (lane.lane_dir / "lane.json").write_text(json.dumps(record), encoding="utf-8")

        fresh = self.supervisor()
        fresh.refresh_lanes()
        self.assertIn("W-A", fresh.malformed_lanes)
        self.assertIn("identity-mismatch", fresh.malformed_lanes["W-A"])

    def test_SUP_DISC_006_a_vanished_directory_does_not_erase_known_work(self):
        """An antivirus scan or half-written save must not delete a lane."""
        import shutil
        running = self.supervisor()
        running.create_lane("W-A", "objective", expect="HANDOFF_W-A_X_TO_ORBIT.md")
        running.refresh_lanes()
        shutil.rmtree(self.root / "lanes" / "W-A")

        running.refresh_lanes()
        self.assertIn("W-A", self.names(running))
        self.assertIn("W-A", running.vanished_lanes)

    def test_SUP_DISC_007_restart_reconstructs_the_same_lane_set(self):
        running = self.supervisor()
        for name in ("W-A", "W-B", "W-C"):
            running.create_lane(name, "objective", expect=f"HANDOFF_{name}_X_TO_ORBIT.md")

        restarted = self.supervisor()
        self.assertEqual(self.names(restarted), ["W-A", "W-B", "W-C"])

    def test_SUP_DISC_008_status_and_cycle_see_the_same_inventory(self):
        running = self.supervisor()
        self.supervisor().create_lane("W-LATE", "objective", expect="HANDOFF_W-LATE_X_TO_ORBIT.md")

        summary = running.status_summary()
        self.assertIn("W-LATE", [l["work_item"] for l in summary["lanes"]])
        self.assertIn("W-LATE", self.names(running))


class ScopeSourceOfTruthTests(unittest.TestCase):
    """Scope must come from the committed config, never from a code default."""

    def test_SUP_SCOPE_001_runtime_scope_matches_the_committed_config(self):
        import json, pathlib, tempfile
        from standalone.operator.supervisor import DEFAULT_CONFIG_PATH, MultiWorkItemSupervisor

        committed = json.loads(pathlib.Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            sup = MultiWorkItemSupervisor(pathlib.Path(tmp), adapter=MagicMock())
            self.assertEqual(sup.workflow_scope, committed["workflow_scope"])
            self.assertEqual(sup.project_scope, committed["project_scope"])
            self.assertEqual(sup.chat_list_name, committed["chat_list_name"])

    def test_SUP_SCOPE_002_a_missing_scope_key_fails_closed(self):
        """A code default would keep working while sourcing a second truth."""
        import tempfile, pathlib
        from standalone.operator.supervisor import MultiWorkItemSupervisor

        for key in ("project_scope", "workflow_scope", "chat_list_name"):
            with tempfile.TemporaryDirectory() as tmp:
                config = _config_without(tmp, key)
                with self.assertRaises(ValueError) as ctx:
                    MultiWorkItemSupervisor(pathlib.Path(tmp) / "state",
                                            adapter=MagicMock(), config_path=config)
                self.assertIn(key, str(ctx.exception))


class TestMultiWorkItemSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mock_adapter = MagicMock()
        self.supervisor = MultiWorkItemSupervisor(self.state_dir, adapter=self.mock_adapter)

    def tearDown(self):
        self.tmp.cleanup()

    def test_workflow_scope_loaded_from_committed_config(self):
        config = load_orbit_config()
        self.assertEqual(self.supervisor.project_scope, config["project_scope"])
        self.assertEqual(self.supervisor.workflow_scope, config["workflow_scope"])
        self.assertEqual(self.supervisor.chat_list_name, config["chat_list_name"])
        self.assertEqual(self.supervisor.workflow_scope, "orbit-m0-live-trial")
        self.assertNotEqual(self.supervisor.workflow_scope, "live_workflow_trial")

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
        self.assertEqual(lane1.record.accepted_action, "DISPATCH_TO_ROLE")
        self.assertEqual(lane1.record.current_endpoint, "windows-worker")

    def test_pm_directive_exact_semantics_hold(self):
        """HOLD directive must transition to STATE_HOLD, must NOT dispatch, and must persist across restart."""
        lane = self.supervisor.create_lane("WORK-HOLD", "Hold test objective")
        self.mock_adapter.deliver.return_value = ChatTransportResult.allow("SEND_BOUNDED_MESSAGE", {}, delivery_state="DELIVERED")

        self.supervisor.step_lane(lane)
        self.assertEqual(lane.record.work_state, STATE_AWAITING_PM_ROUTING)

        # PM replies with HOLD
        self.mock_adapter.focus.return_value = MagicMock(ok=True)
        directive_text = (
            "ChatGPT said:\n"
            "```\n"
            "ORBIT_DIRECTIVE\n"
            "version: 0.1\n"
            f"request_id: {lane.record.pending_request_id}\n"
            "directive_id: dir-hold-001\n"
            "work_item: WORK-HOLD\n"
            "action: HOLD\n"
            "reason: awaiting user confirmation\n"
            "```"
        )
        self.mock_adapter.driver.read_transcript_tail.return_value = MagicMock(ok=True, data={"text": directive_text})

        step_res = self.supervisor.step_lane(lane)
        self.assertEqual(step_res["action"], "PM_DIRECTED_HOLD")
        self.assertEqual(lane.record.work_state, STATE_HOLD)
        self.assertEqual(lane.record.accepted_action, "HOLD")
        self.assertEqual(lane.record.accepted_directive_id, "dir-hold-001")

        # Subsequent steps must be IDLE / NO-OP, never dispatch
        step_res_again = self.supervisor.step_lane(lane)
        self.assertEqual(step_res_again["action"], "IDLE")
        self.assertEqual(lane.record.work_state, STATE_HOLD)

        # Verify state persistence across restart
        restarted = MultiWorkItemSupervisor(self.state_dir, adapter=self.mock_adapter)
        reloaded_lane = restarted.get_lane("WORK-HOLD")
        self.assertEqual(reloaded_lane.record.work_state, STATE_HOLD)
        self.assertEqual(reloaded_lane.record.accepted_action, "HOLD")

    def test_pm_directive_exact_semantics_stop(self):
        """STOP directive must halt the lane, create STOP file, and must NOT dispatch."""
        lane = self.supervisor.create_lane("WORK-STOP", "Stop test objective")
        self.mock_adapter.deliver.return_value = ChatTransportResult.allow("SEND_BOUNDED_MESSAGE", {}, delivery_state="DELIVERED")

        self.supervisor.step_lane(lane)
        self.assertEqual(lane.record.work_state, STATE_AWAITING_PM_ROUTING)

        # PM replies with STOP
        self.mock_adapter.focus.return_value = MagicMock(ok=True)
        directive_text = (
            "ChatGPT said:\n"
            "```\n"
            "ORBIT_DIRECTIVE\n"
            "version: 0.1\n"
            f"request_id: {lane.record.pending_request_id}\n"
            "directive_id: dir-stop-001\n"
            "work_item: WORK-STOP\n"
            "action: STOP\n"
            "reason: security policy violation\n"
            "```"
        )
        self.mock_adapter.driver.read_transcript_tail.return_value = MagicMock(ok=True, data={"text": directive_text})

        step_res = self.supervisor.step_lane(lane)
        self.assertEqual(step_res["action"], "PM_DIRECTED_STOP")
        self.assertEqual(lane.record.work_state, STATE_STOPPED)
        self.assertEqual(lane.record.accepted_action, "STOP")
        self.assertTrue(lane.stopped())
        self.assertTrue(lane.stop_path.is_file())

        # Subsequent steps must return STOPPED
        step_res_again = self.supervisor.step_lane(lane)
        self.assertEqual(step_res_again["action"], "STOPPED")

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
