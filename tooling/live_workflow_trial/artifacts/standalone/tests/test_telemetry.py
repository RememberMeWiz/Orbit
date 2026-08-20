"""Tests for Orbit workflow telemetry and efficiency metrics."""
import tempfile
import unittest
from pathlib import Path

from standalone.operator.telemetry import HopTelemetry, TelemetryStore


class TestTelemetryStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "telemetry.jsonl"
        self.store = TelemetryStore(self.log_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_summary(self):
        s = self.store.summary()
        self.assertEqual(s["total_hops"], 0)
        self.assertEqual(s["zero_courier_rate"], 1.0)
        self.assertEqual(s["zero_click_rate"], 1.0)

    def test_record_and_summary(self):
        h1 = HopTelemetry(
            work_item="WORK-001",
            hop_id="hop-1",
            target_endpoint="windows-worker",
            pm_wait_seconds=12.5,
            dispatch_seconds=2.0,
            worker_response_seconds=45.0,
            collect_seconds=1.5,
            human_ui_actions=0,
            courier_actions=0,
            work_mode_escalations=0,
            result="SUCCESS",
        )
        h2 = HopTelemetry(
            work_item="WORK-002",
            hop_id="hop-2",
            target_endpoint="architecture-tl",
            pm_wait_seconds=20.0,
            dispatch_seconds=3.0,
            worker_response_seconds=60.0,
            collect_seconds=2.0,
            human_ui_actions=0,
            courier_actions=0,
            work_mode_escalations=0,
            result="SUCCESS",
        )
        h3 = HopTelemetry(
            work_item="WORK-003",
            hop_id="hop-3",
            target_endpoint="qa-safety",
            pm_wait_seconds=30.0,
            dispatch_seconds=2.5,
            worker_response_seconds=10.0,
            collect_seconds=0.0,
            human_ui_actions=0,
            courier_actions=0,
            work_mode_escalations=0,
            result="BLOCKED",
            blocker_reason="syntax-check-failed",
        )
        self.store.record(h1)
        self.store.record(h2)
        self.store.record(h3)

        records = self.store.all_records()
        self.assertEqual(len(records), 3)

        s = self.store.summary()
        self.assertEqual(s["total_hops"], 3)
        self.assertEqual(s["successful_hops"], 2)
        self.assertEqual(s["blocked_hops"], 1)
        self.assertEqual(s["zero_courier_rate"], 1.0)
        self.assertEqual(s["zero_click_rate"], 1.0)
        self.assertAlmostEqual(s["avg_pm_wait_seconds"], 20.83, delta=0.1)
        self.assertEqual(s["median_pm_wait_seconds"], 20.0)
        self.assertEqual(s["endpoint_counts"]["windows-worker"], 1)
        self.assertEqual(s["endpoint_counts"]["architecture-tl"], 1)
        self.assertEqual(s["endpoint_counts"]["qa-safety"], 1)
        self.assertEqual(s["blocker_reasons"]["syntax-check-failed"], 1)

    def test_format_report(self):
        h = HopTelemetry(
            work_item="WORK-001",
            hop_id="hop-1",
            target_endpoint="windows-worker",
            pm_wait_seconds=10.0,
            dispatch_seconds=2.0,
            worker_response_seconds=30.0,
            collect_seconds=1.0,
            result="SUCCESS",
        )
        self.store.record(h)
        report = self.store.format_report()
        self.assertIn("ORBIT WORKFLOW EFFICIENCY METRICS", report)
        self.assertIn("Total Workflow Hops        : 1", report)
        self.assertIn("Zero-Courier Rate        : 100.0%", report)
        self.assertIn("Zero-Click Rate          : 100.0%", report)


if __name__ == "__main__":
    unittest.main()
