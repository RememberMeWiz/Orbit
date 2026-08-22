"""Tests for Orbit overnight unattended runner."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from standalone.operator.lane import STATE_COMPLETED, STATE_INITIALIZED
from standalone.operator.overnight import OvernightRunner
from standalone.operator.supervisor import MultiWorkItemSupervisor


class TestOvernightRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mock_adapter = MagicMock()
        self.supervisor = MultiWorkItemSupervisor(self.state_dir, adapter=self.mock_adapter)

    def tearDown(self):
        self.tmp.cleanup()

    def test_overnight_unattended_cycle_and_logging(self):
        with patch.object(self.supervisor, "check_surface") as mock_surface:
            mock_surface.return_value = {"ok": True, "drivable": True, "status": "READY"}

            lane = self.supervisor.create_lane("WORK-OVERNIGHT", "Overnight test task")

            runner = OvernightRunner(
                self.supervisor,
                poll_interval=0.01,
                idle_threshold=0.0, max_cycles=3,
                sleeper=lambda s: None,
            )

            res = runner.run()
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "COMPLETED")
            self.assertEqual(res["cycles"], 3)

            # Verify log files created
            self.assertTrue(runner.log_file.is_file())
            self.assertTrue(runner.events_file.is_file())

            log_content = runner.log_file.read_text(encoding="utf-8")
            self.assertIn("OVERNIGHT_STARTED", log_content)
            self.assertIn("OVERNIGHT_FINISHED", log_content)

    def test_overnight_stops_on_global_stop(self):
        with patch.object(self.supervisor, "check_surface") as mock_surface:
            mock_surface.return_value = {"ok": True, "drivable": True, "status": "READY"}

            # Place global stop before running
            self.supervisor.stop_all()

            runner = OvernightRunner(
                self.supervisor,
                poll_interval=0.01,
                idle_threshold=0.0, max_cycles=5,
                sleeper=lambda s: None,
            )

            res = runner.run()
            self.assertTrue(res["ok"])
            self.assertEqual(res["cycles"], 0)


if __name__ == "__main__":
    unittest.main()
