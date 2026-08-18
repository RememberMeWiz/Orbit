from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from windows.adapters.place_packet import PlacePacketExecutor


HANDOFF = """# Orbit Handoff\n\n## Header\n- Work Item: M0-WF-WIN-001\n- From: WORKER\n- To: TL\n- Status: COMPLETE\n- Handoff ID: {handoff_id}\n- Sequence: 1\n\n## Executive Summary\nIntegration hardening test.\n"""


class IntegrationContractTests(unittest.TestCase):
    def setUp(self):
        source_root = Path(__file__).resolve().parents[3]
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orbit"
        shutil.copytree(source_root / "artifacts", self.root / "artifacts")
        state = self.root / "artifacts/sample_workspace/state.json"
        if state.exists():
            state.unlink()
        self.manifest = load_manifest(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_INT_001_manifest_executor_catalog_is_exactly_PLACE_PACKET(self):
        self.assertEqual(self.manifest["allowed_executor_operations"], ["PLACE_PACKET"])
        executor = PlacePacketExecutor(self.root, self.manifest)
        ok, result, _ = executor.place_packet("TL", {
            "handoff_id": "int-1",
            "artifact_digest": "0" * 64,
            "to": "TL",
        })
        self.assertTrue(ok)
        self.assertEqual(result, "PREPARED")
        self.assertEqual(executor.operations, ["PLACE_PACKET"])

    def test_INT_002_destination_parent_traversal_fails_closed(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest["destinations"]["TL"] = "../escape"
        manifest["role_destination_registry"]["TL"]["endpoint_ref"] = "../escape"
        executor = PlacePacketExecutor(self.root, manifest)
        ok, result, destination = executor.place_packet("TL", {
            "handoff_id": "int-2",
            "artifact_digest": "0" * 64,
            "to": "TL",
        })
        self.assertFalse(ok)
        self.assertEqual(result, "FAILED_FINAL:destination-parent-traversal-not-allowed")
        self.assertEqual(destination, "none")
        self.assertFalse((self.root / "escape").exists())

    def test_INT_003_handoff_id_never_becomes_path_component(self):
        inbox = self.root / "artifacts/sample_workspace/inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        p = inbox / "HANDOFF_M0-WF-WIN-001_WORKER_TO_TL.md"
        attacker_id = "../../outside"
        p.write_text(HANDOFF.format(handoff_id=attacker_id), encoding="utf-8")
        executor = PlacePacketExecutor(self.root, self.manifest)
        engine = WorkflowEngine(self.root, self.manifest, executor)
        result = engine.process(p)
        self.assertEqual(result["validation_result"], "accepted")
        self.assertFalse((self.root / "outside").exists())
        outbox = self.root / "artifacts/sample_workspace/outboxes/TL"
        files = list(outbox.glob("NEXT_*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("..", files[0].name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
