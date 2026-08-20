"""Tests for Orbit central operator CLI and launcher."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from standalone.operator.cli import default_state_dir, main


class TestOperatorLauncher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_state_dir(self):
        with patch.dict("os.environ", {"ORBIT_STATE_DIR": str(self.state_dir)}):
            self.assertEqual(default_state_dir(), self.state_dir)

    def test_status_json_command(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with patch("standalone.operator.supervisor.AccessibilityGuard") as mock_guard:
                mock_guard.return_value.ensure.return_value.to_dict.return_value = {"ok": False, "status": "UNAVAILABLE"}
                code = main(["--state-dir", str(self.state_dir), "--json", "status"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("total_lanes"), 0)

    def test_lanes_command(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--state-dir", str(self.state_dir), "lanes"])
        self.assertEqual(code, 0)
        self.assertIn("No active or registered workflow lanes.", buf.getvalue())

    def test_metrics_command(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--state-dir", str(self.state_dir), "metrics"])
        self.assertEqual(code, 0)
        self.assertIn("ORBIT WORKFLOW EFFICIENCY METRICS", buf.getvalue())

    def test_insights_command(self):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--state-dir", str(self.state_dir), "insights"])
        self.assertEqual(code, 0)
        self.assertIn("ORBIT WORKFLOW SELF-IMPROVEMENT INSIGHTS", buf.getvalue())

    def test_stop_global_and_lane(self):
        with patch("standalone.operator.supervisor.MultiWorkItemSupervisor.step_lane") as mock_step:
            mock_step.return_value = {"action": "MOCKED", "state": "INITIALIZED"}
            # Create a lane first
            main(["--state-dir", str(self.state_dir), "work", "Test objective", "--work-item", "WORK-TEST"])

        # Stop specific lane
        code = main(["--state-dir", str(self.state_dir), "stop", "WORK-TEST"])
        self.assertEqual(code, 0)
        lane_stop = self.state_dir / "lanes" / "WORK-TEST" / "STOP"
        self.assertTrue(lane_stop.is_file())

        # Stop global
        code = main(["--state-dir", str(self.state_dir), "stop"])
        self.assertEqual(code, 0)
        global_stop = self.state_dir / "STOP"
        self.assertTrue(global_stop.is_file())


if __name__ == "__main__":
    unittest.main()
