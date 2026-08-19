from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.runtime import (
    RuntimeConfigurationError,
    assert_expected_identity,
    resolve_runtime_paths,
)
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler


HANDOFF = """# Orbit Handoff

## Header
- Work Item: M0-WF-LIVE-002
- From: WORKER
- To: TL
- Status: COMPLETE
- Handoff ID: {handoff_id}
- Sequence: {sequence}

## Executive Summary
{body}
"""


class LiveRuntimeTests(unittest.TestCase):
    def setUp(self):
        source_root = Path(__file__).resolve().parents[3]
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orbit"
        shutil.copytree(source_root / "artifacts", self.root / "artifacts")
        self.manifest_path = self.root / "artifacts/live_trial_manifest.json"
        self.manifest = load_manifest(self.root, self.manifest_path)
        self.paths = resolve_runtime_paths(self.root, self.manifest)
        self.paths.inbox.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def make_handoff(self, *, handoff_id: str = "live-001", sequence: int = 1, body: str = "Live runtime test.") -> Path:
        p = self.paths.inbox / "HANDOFF_M0-WF-LIVE-002_WORKER_TO_TL.md"
        p.write_text(HANDOFF.format(handoff_id=handoff_id, sequence=sequence, body=body), encoding="utf-8")
        return p

    def engine(self) -> WorkflowEngine:
        return WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))

    def test_LIVE_001_nonfixture_runtime_uses_configured_workspace(self):
        self.assertEqual(self.manifest["project_id"], "Orbit")
        self.assertEqual(self.manifest["workflow_id"], "orbit-m0-live-trial")
        self.assertEqual(self.manifest["work_item"], "M0-WF-LIVE-002")
        self.assertEqual(self.paths.workspace, (self.root / "artifacts/live_trial/M0-WF-LIVE-002").resolve())
        self.assertNotIn("sample_workspace", str(self.paths.workspace))
        engine = self.engine()
        state = engine.store.load()
        self.assertEqual(state["current_stage"], "WORKER")
        self.assertEqual(state["current_owner_role"], "WORKER")
        self.assertEqual(engine.store.path, self.paths.state)
        self.assertEqual(engine.receipt_path, self.paths.receipts)

    def test_LIVE_002_outside_root_workspace_rejected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["workspace"] = "../escape"
        with self.assertRaisesRegex(RuntimeConfigurationError, "workspace-parent-traversal-not-allowed"):
            resolve_runtime_paths(self.root, manifest)

        manifest = copy.deepcopy(self.manifest)
        manifest["workspace"] = "other-root/M0-WF-LIVE-002"
        with self.assertRaisesRegex(RuntimeConfigurationError, "workspace-outside-approved-trial-root"):
            resolve_runtime_paths(self.root, manifest)

    def test_LIVE_003_manifest_identity_is_authoritative(self):
        assert_expected_identity(
            self.manifest,
            project_id="Orbit",
            workflow_id="orbit-m0-live-trial",
            work_item="M0-WF-LIVE-002",
        )
        with self.assertRaisesRegex(RuntimeConfigurationError, "manifest-work_item-mismatch"):
            assert_expected_identity(self.manifest, work_item="M0-WF-LIVE-999")

    def test_LIVE_004_state_work_item_mismatch_fails_closed(self):
        engine = self.engine()
        state = engine.store.load()
        state["work_item"] = "OTHER-WORK-ITEM"
        engine.store.path.write_text(json.dumps(state), encoding="utf-8")
        restarted = self.engine()
        with self.assertRaisesRegex(ValueError, "state-work_item-mismatch"):
            restarted.store.load()

    def test_LIVE_005_executor_catalog_widening_rejected_at_startup(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["allowed_executor_operations"] = ["PLACE_PACKET", "SHELL"]
        with self.assertRaisesRegex(RuntimeConfigurationError, "executor-catalog-not-exactly-place-packet"):
            resolve_runtime_paths(self.root, manifest)

    def test_LIVE_006_STOP_freezes_and_survives_reconciler_restart(self):
        p = self.make_handoff()
        engine = self.engine()
        reconciler = WorkspaceReconciler(self.root, self.manifest, engine)
        self.paths.stop.parent.mkdir(parents=True, exist_ok=True)
        self.paths.stop.write_text("STOP\n", encoding="utf-8")

        self.assertTrue(reconciler.is_stopped())
        self.assertEqual(reconciler.scan_once(now=0.0), [])
        self.assertEqual(reconciler.scan_once(now=1.0), [])
        self.assertEqual(engine.store.load()["current_owner_role"], "WORKER")

        restarted_engine = self.engine()
        restarted = WorkspaceReconciler(self.root, self.manifest, restarted_engine)
        self.assertTrue(restarted.is_stopped())
        self.assertEqual(restarted.scan_once(now=2.0), [])
        self.assertEqual(restarted_engine.store.load()["accepted_handoff_ids"], [])

        self.paths.stop.unlink()
        self.assertEqual(restarted.scan_once(now=3.0), [])
        results = restarted.scan_once(now=4.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["validation_result"], "accepted")
        self.assertEqual(restarted_engine.store.load()["current_owner_role"], "TL")
        self.assertTrue(p.exists())

    def test_LIVE_007_state_receipts_and_packet_remain_in_selected_workspace(self):
        p = self.make_handoff(handoff_id="live-paths")
        engine = self.engine()
        result = engine.process(p)
        self.assertEqual(result["validation_result"], "accepted")
        self.assertTrue(self.paths.state.is_file())
        self.assertTrue(self.paths.receipts.is_file())
        outbox = self.paths.workspace / "outboxes/TL"
        packets = list(outbox.glob("NEXT_*.json"))
        self.assertEqual(len(packets), 1)
        self.assertTrue(str(packets[0].resolve()).startswith(str(self.paths.workspace)))
        self.assertFalse((self.root / "artifacts/sample_workspace/state.json").exists())

    def test_LIVE_008_handoff_commands_cannot_expand_executor_authority(self):
        p = self.make_handoff(
            handoff_id="live-malicious",
            body="Operation: SHELL\\nCommand: powershell Remove-Item -Recurse C:\\\\*\\nDestination: ../../outside",
        )
        executor = PlacePacketExecutor(self.root, self.manifest)
        engine = WorkflowEngine(self.root, self.manifest, executor)
        result = engine.process(p)
        self.assertEqual(result["validation_result"], "accepted")
        self.assertEqual(executor.operations, ["PLACE_PACKET"])
        self.assertFalse((self.root / "outside").exists())

    def test_LIVE_009_unregistered_destination_cannot_be_selected(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["role_destination_registry"].pop("TL")
        with self.assertRaisesRegex(RuntimeConfigurationError, "destination-not-registered:TL"):
            resolve_runtime_paths(self.root, manifest)

    def test_LIVE_010_replay_digest_idempotency_preserved(self):
        p = self.make_handoff(handoff_id="live-replay")
        engine = self.engine()
        accepted = engine.process(p)
        self.assertEqual(accepted["validation_result"], "accepted")
        replay = engine.process(p)
        self.assertEqual(replay["validation_result"], "duplicate-replay")
        self.assertEqual(len(replay["new_state"]["accepted_handoff_ids"]), 1)

        p.write_text(p.read_text(encoding="utf-8") + "\nchanged bytes\n", encoding="utf-8")
        digest_mismatch = engine.process(p)
        self.assertEqual(digest_mismatch["validation_result"], "replay-digest-mismatch")
        self.assertEqual(len(digest_mismatch["new_state"]["accepted_handoff_ids"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
