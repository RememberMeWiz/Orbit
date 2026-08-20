"""Tests for Orbit workflow self-improvement insights engine."""
import tempfile
import unittest
from pathlib import Path

from standalone.operator.insights import WorkflowInsightsAnalyzer
from standalone.operator.telemetry import HopTelemetry, TelemetryStore


class TestWorkflowInsights(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.tmp.name) / "telemetry.jsonl"
        self.store = TelemetryStore(self.log_path)
        self.analyzer = WorkflowInsightsAnalyzer(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_insights(self):
        insights = self.analyzer.analyze()
        self.assertEqual(len(insights), 1)
        self.assertEqual(insights[0].category, "EFFICIENCY")
        self.assertIn("No workflow hops", insights[0].observation)

    def test_high_pm_wait_insight(self):
        for i in range(5):
            self.store.record(
                HopTelemetry(
                    work_item=f"WORK-{i}",
                    hop_id=f"hop-{i}",
                    target_endpoint="windows-worker",
                    pm_wait_seconds=900.0,  # 15 minutes
                    worker_response_seconds=30.0,
                    result="SUCCESS",
                )
            )
        insights = self.analyzer.analyze()
        latency_insights = [i for i in insights if i.category == "LATENCY"]
        self.assertTrue(len(latency_insights) >= 1)
        self.assertIn("batching multi-hop assignments", latency_insights[0].proposal)

    def test_endpoint_bottleneck_insight(self):
        # 2 failures on qa-safety
        self.store.record(
            HopTelemetry(
                work_item="WORK-1",
                hop_id="hop-1",
                target_endpoint="qa-safety",
                pm_wait_seconds=10.0,
                result="BLOCKED",
                blocker_reason="type-mismatch",
            )
        )
        self.store.record(
            HopTelemetry(
                work_item="WORK-2",
                hop_id="hop-2",
                target_endpoint="qa-safety",
                pm_wait_seconds=10.0,
                result="BLOCKED",
                blocker_reason="type-mismatch",
            )
        )
        insights = self.analyzer.analyze()
        bottlenecks = [i for i in insights if i.category == "BOTTLENECK"]
        self.assertEqual(len(bottlenecks), 1)
        self.assertIn("Endpoint 'qa-safety'", bottlenecks[0].observation)
        self.assertIn("Inspect prompt instructions", bottlenecks[0].proposal)

    def test_transcript_savings_insight(self):
        self.store.record(
            HopTelemetry(
                work_item="WORK-1",
                hop_id="hop-1",
                target_endpoint="windows-worker",
                pm_wait_seconds=10.0,
                courier_actions=0,
                work_mode_escalations=0,
                result="SUCCESS",
            )
        )
        insights = self.analyzer.analyze()
        eff = [i for i in insights if i.category == "EFFICIENCY"]
        self.assertTrue(any("Transcript collection satisfied" in i.observation for i in eff))

    def test_format_insights(self):
        self.store.record(
            HopTelemetry(
                work_item="WORK-1",
                hop_id="hop-1",
                target_endpoint="windows-worker",
                pm_wait_seconds=10.0,
                result="SUCCESS",
            )
        )
        text = self.analyzer.format_insights()
        self.assertIn("ORBIT WORKFLOW SELF-IMPROVEMENT INSIGHTS", text)
        self.assertIn("Orbit never automatically alters routing rules", text)


if __name__ == "__main__":
    unittest.main()
