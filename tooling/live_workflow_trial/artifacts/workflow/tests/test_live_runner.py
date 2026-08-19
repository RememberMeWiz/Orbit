"""Integration tests for the long-running live_runner daemon.

LIVE-003 delivered a bootstrapper that creates real work-item workspaces under
artifacts/live_trial/<work item>/. The daemon the Product Owner actually watches
was never updated to consume one: it hard-coded artifacts/sample_workspace and
had no way to select a manifest or bind a launch identity. These tests pin the
bootstrapper -> runner seam so the two accepted work items stay integrated.

The runner is exercised as a subprocess, the way an operator runs it.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workflow.core.bootstrap import bootstrap_workspace


PACKAGE_ROOT = Path(__file__).resolve().parents[3]
RUNNER = PACKAGE_ROOT / "live_runner.py"

HANDOFF = """# Orbit Handoff

## Header
- Work Item: M0-WF-LIVE-003
- From: WORKER
- To: TL
- Status: COMPLETE
- Handoff ID: {handoff_id}
- Sequence: {sequence}

## Executive Summary
Live runner integration fixture.
"""


class LiveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orbit"
        shutil.copytree(PACKAGE_ROOT / "artifacts", self.root / "artifacts")

        config_path = self.root / "artifacts/live003_bootstrap_config.json"
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        workspace = self.root / "artifacts/live_trial/M0-WF-LIVE-003"
        if workspace.exists():
            shutil.rmtree(workspace)

        self.result = bootstrap_workspace(
            self.root,
            self.config,
            project_id="Orbit",
            workflow_id="orbit-m0-live-trial",
            work_item="M0-WF-LIVE-003",
        )
        self.assertEqual(self.result["status"], "INITIALIZED")
        self.manifest_path = Path(self.result["manifest_path"])
        self.workspace = Path(self.result["workspace"])
        self.inbox = Path(self.result["inbox"])
        self.state_path = Path(self.result["state_path"])
        self.stop_path = Path(self.result["stop_path"])

    def tearDown(self):
        self.tmp.cleanup()

    def run_runner(self, *extra: str, iterations: int = 6):
        # stable_window_seconds is 0.25, so an artifact only becomes eligible on a
        # later poll than the one that first observed it. Poll long enough that a
        # settled artifact is always picked up.
        command = [
            sys.executable, str(RUNNER),
            "--root", str(self.root),
            "--manifest", str(self.manifest_path),
            "--interval", "0.1",
            "--max-iterations", str(iterations),
        ]
        command.extend(extra)
        return subprocess.run(command, text=True, capture_output=True, cwd=str(PACKAGE_ROOT))

    def seed(self, *, handoff_id: str = "runner-001", sequence: int = 1) -> Path:
        p = self.inbox / "HANDOFF_M0-WF-LIVE-003_WORKER_TO_TL.md"
        p.write_text(HANDOFF.format(handoff_id=handoff_id, sequence=sequence), encoding="utf-8")
        return p

    @staticmethod
    def snapshot_tree(root: Path) -> dict:
        if not root.exists():
            return {}
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def tl_packets(self):
        return list((self.workspace / "outboxes/TL").glob("NEXT_*.json"))

    def test_RUNNER_001_drives_bootstrapped_live_workspace(self):
        """The daemon must be able to run the workspace the bootstrapper creates."""
        self.seed()
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        state = self.state()
        self.assertEqual(state["current_owner_role"], "TL")
        self.assertEqual(state["work_item"], "M0-WF-LIVE-003")
        self.assertEqual(state["workflow_id"], "orbit-m0-live-trial")
        self.assertEqual(len(self.tl_packets()), 1)
        self.assertTrue(Path(self.result["receipts_path"]).is_file())

    def test_RUNNER_002_launch_identity_mismatch_fails_closed(self):
        self.seed()
        proc = self.run_runner("--work-item", "M0-WF-SOMETHING-ELSE")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("manifest-work_item-mismatch", proc.stderr)
        self.assertEqual(self.state()["current_owner_role"], "WORKER")
        self.assertEqual(self.tl_packets(), [])

    def test_RUNNER_003_matching_identity_is_accepted(self):
        self.seed()
        proc = self.run_runner(
            "--project-id", "Orbit",
            "--workflow-id", "orbit-m0-live-trial",
            "--work-item", "M0-WF-LIVE-003",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state()["current_owner_role"], "TL")

    def test_RUNNER_004_stop_freezes_advancement_across_restart(self):
        self.seed()
        self.stop_path.write_text("stopped by operator\n", encoding="utf-8")

        for _ in range(2):
            proc = self.run_runner(iterations=2)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.state()["current_owner_role"], "WORKER")
            self.assertEqual(self.tl_packets(), [])

        self.stop_path.unlink()
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.state()["current_owner_role"], "TL")
        self.assertEqual(len(self.tl_packets()), 1)

    def test_RUNNER_005_restart_does_not_repeat_the_transition(self):
        self.seed()
        self.run_runner()
        first = self.state()
        self.assertEqual(first["current_owner_role"], "TL")

        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        second = self.state()
        self.assertEqual(second["current_owner_role"], "TL")
        self.assertEqual(second["accepted_handoff_ids"], first["accepted_handoff_ids"])
        self.assertEqual(len(self.tl_packets()), 1)

    def test_RUNNER_006_does_not_write_into_fixture_workspace(self):
        fixture = self.root / "artifacts/sample_workspace"
        before = self.snapshot_tree(fixture)
        self.seed()
        proc = self.run_runner()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self.snapshot_tree(fixture),
            before,
            "live runner wrote into the fixture sample_workspace",
        )
        self.assertNotIn("sample_workspace", str(self.workspace))


if __name__ == "__main__":
    unittest.main(verbosity=2)
