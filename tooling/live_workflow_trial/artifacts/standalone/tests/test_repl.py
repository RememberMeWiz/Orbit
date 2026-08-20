"""Tests for Orbit Operator REPL console."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from standalone.operator.repl import OperatorRepl
from standalone.operator.supervisor import MultiWorkItemSupervisor


class TestOperatorRepl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mock_adapter = MagicMock()
        self.supervisor = MultiWorkItemSupervisor(self.state_dir, adapter=self.mock_adapter)
        self.output_lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def _output_capture(self, text: str) -> None:
        self.output_lines.append(text)

    def test_repl_commands(self):
        inputs = iter([
            "status",
            "work Test new objective",
            "lanes",
            "metrics",
            "insights",
            "help",
            "quit",
        ])

        repl = OperatorRepl(
            self.supervisor,
            input_fn=lambda prompt="": next(inputs),
            output_fn=self._output_capture,
        )

        repl.run()

        output = "\n".join(self.output_lines)
        self.assertIn("ORBIT OPERATOR CONSOLE", output)
        self.assertIn("--- System Health ---", output)
        self.assertIn("Registered new work item:", output)
        self.assertIn("ORBIT WORKFLOW EFFICIENCY METRICS", output)
        self.assertIn("Available Commands:", output)
        self.assertIn("Exiting Orbit console.", output)


if __name__ == "__main__":
    unittest.main()
