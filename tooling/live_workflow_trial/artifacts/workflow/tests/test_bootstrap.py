from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from workflow.core.bootstrap import BootstrapError, bootstrap_workspace
from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.runtime import resolve_runtime_paths
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler


HANDOFF = """# Orbit Handoff

## Header
- Work Item: M0-WF-LIVE-003
- From: WORKER
- To: TL
- Status: COMPLETE
- Handoff ID: {handoff_id}
- Sequence: {sequence}

## Executive Summary
{body}
"""


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        source_root = Path(__file__).resolve().parents[3]
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orbit"
        shutil.copytree(source_root / "artifacts", self.root / "artifacts")
        self.config_path = self.root / "artifacts/live003_bootstrap_config.json"
        self.manifest = json.loads(self.config_path.read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def bootstrap(self, manifest=None, **identity):
        manifest = copy.deepcopy(manifest or self.manifest)
        return bootstrap_workspace(
            self.root,
            manifest,
            project_id=identity.get("project_id", "Orbit"),
            workflow_id=identity.get("workflow_id", "orbit-m0-live-trial"),
            work_item=identity.get("work_item", "M0-WF-LIVE-003"),
        )

    def loaded_runtime(self):
        manifest_path = self.root / "artifacts/live_trial/M0-WF-LIVE-003/manifest.json"
        manifest = load_manifest(self.root, manifest_path)
        paths = resolve_runtime_paths(self.root, manifest)
        executor = PlacePacketExecutor(self.root, manifest)
        engine = WorkflowEngine(self.root, manifest, executor)
        return manifest, paths, executor, engine

    def make_handoff(self, paths, handoff_id="live003-1", sequence=1, body="Bootstrap runtime test."):
        p = paths.inbox / "HANDOFF_M0-WF-LIVE-003_WORKER_TO_TL.md"
        p.write_text(HANDOFF.format(handoff_id=handoff_id, sequence=sequence, body=body), encoding="utf-8")
        return p

    def test_BOOT_001_fresh_real_work_item_initializes_and_runner_loads(self):
        result = self.bootstrap()
        self.assertEqual(result["status"], "INITIALIZED")
        self.assertEqual(result["executor_catalog"], ["PLACE_PACKET"])
        self.assertNotIn("sample_workspace", result["workspace"])

        manifest_path = Path(result["manifest_path"])
        state_path = Path(result["state_path"])
        receipts_path = Path(result["receipts_path"])
        self.assertTrue(manifest_path.is_file())
        self.assertTrue(state_path.is_file())
        self.assertTrue(receipts_path.is_file())
        self.assertEqual(receipts_path.read_text(encoding="utf-8"), "")

        persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for field in ("project_id", "workflow_id", "work_item"):
            self.assertEqual(state[field], persisted_manifest[field])
            self.assertEqual(state[field], self.manifest[field])
        self.assertEqual(state["current_stage"], "WORKER")
        self.assertEqual(state["current_owner_role"], "WORKER")

        self.assertEqual(set(self.manifest["destinations"]), set(self.manifest["role_destination_registry"]))
        for value in self.manifest["destinations"].values():
            self.assertTrue((self.root / "artifacts" / value).is_dir())
        self.assertFalse((self.root / "artifacts/live_trial/M0-WF-LIVE-003/outboxes/GHOST").exists())

        loaded, paths, _, engine = self.loaded_runtime()
        self.assertEqual(loaded["work_item"], "M0-WF-LIVE-003")
        self.assertEqual(engine.store.load()["work_item"], "M0-WF-LIVE-003")
        self.assertEqual(paths.stop, paths.workspace / "STOP")
        self.assertTrue(paths.stop.parent.is_dir())

    def test_BOOT_002_outside_root_workspace_rejected_without_creation(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["workspace"] = "../outside"
        manifest["inbox"] = "../outside/inbox"
        with self.assertRaises(BootstrapError):
            self.bootstrap(manifest)
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse((self.root / "artifacts/live_trial/M0-WF-LIVE-003/state.json").exists())

    def test_BOOT_003_reparse_escape_rejected_with_zero_outside_writes(self):
        live_root = self.root / "artifacts/live_trial"
        live_root.mkdir(parents=True, exist_ok=True)
        outside = Path(self.tmp.name) / "outside-junction-target"
        outside.mkdir(parents=True, exist_ok=True)
        link = live_root / "escape"
        if os.name == "nt":
            try:
                import _winapi
                _winapi.CreateJunction(str(outside), str(link))
            except Exception as exc:  # pragma: no cover - native suite has non-skipped junction proof
                self.skipTest(f"could not create Windows junction fixture: {exc}")
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            manifest = copy.deepcopy(self.manifest)
            prefix = "live_trial/escape/M0-WF-LIVE-003"
            manifest["workspace"] = prefix
            manifest["inbox"] = f"{prefix}/inbox"
            for key in list(manifest["destinations"]):
                suffix = {
                    "WORKER": "outboxes/WORKER", "TL": "outboxes/TL", "QA": "outboxes/QA", "PM": "outboxes/PM",
                    "BLOCKER": "escalation", "DECISION": "decisions",
                }[key]
                endpoint = f"{prefix}/{suffix}"
                manifest["destinations"][key] = endpoint
                manifest["role_destination_registry"][key]["endpoint_ref"] = endpoint
            with self.assertRaises(BootstrapError):
                self.bootstrap(manifest)
            self.assertEqual(list(outside.rglob("*")), [])
        finally:
            if link.exists() or link.is_symlink() or (getattr(link, "is_junction", lambda: False)()):
                link.rmdir() if link.is_dir() and not link.is_symlink() else link.unlink()

    def test_BOOT_004_manifest_work_item_launch_binding_mismatch_fails_before_creation(self):
        with self.assertRaisesRegex(BootstrapError, "manifest-work_item-mismatch"):
            self.bootstrap(work_item="M0-WF-LIVE-999")
        self.assertFalse((self.root / "artifacts/live_trial/M0-WF-LIVE-003").exists())

    def test_BOOT_005_executor_catalog_widening_fails_closed(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["allowed_executor_operations"] = ["PLACE_PACKET", "SHELL"]
        with self.assertRaisesRegex(BootstrapError, "executor-catalog-not-exactly-place-packet"):
            self.bootstrap(manifest)
        self.assertFalse((self.root / "artifacts/live_trial/M0-WF-LIVE-003/state.json").exists())

    def test_BOOT_006_duplicate_or_unbound_destination_fails_closed(self):
        duplicate = copy.deepcopy(self.manifest)
        duplicate["destinations"]["QA"] = duplicate["destinations"]["TL"]
        duplicate["role_destination_registry"]["QA"]["endpoint_ref"] = duplicate["destinations"]["TL"]
        with self.assertRaisesRegex(BootstrapError, "duplicate-destination-endpoint"):
            self.bootstrap(duplicate)

        missing = copy.deepcopy(self.manifest)
        missing["destinations"].pop("TL")
        missing["role_destination_registry"].pop("TL")
        with self.assertRaisesRegex(BootstrapError, "missing-destination-for-role:TL"):
            self.bootstrap(missing)

        unbound = copy.deepcopy(self.manifest)
        unbound["role_destination_registry"].pop("TL")
        with self.assertRaisesRegex(BootstrapError, "destination-not-registered:TL"):
            self.bootstrap(unbound)
        self.assertFalse((self.root / "artifacts/live_trial/M0-WF-LIVE-003/state.json").exists())

    def test_BOOT_007_incompatible_existing_state_is_rejected(self):
        self.bootstrap()
        state_path = self.root / "artifacts/live_trial/M0-WF-LIVE-003/state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["work_item"] = "OTHER-WORK-ITEM"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(BootstrapError, "state-work_item-mismatch"):
            self.bootstrap()

    def test_BOOT_008_existing_workflow_material_without_manifest_is_rejected(self):
        workspace = self.root / "artifacts/live_trial/M0-WF-LIVE-003"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "state.json").write_text(json.dumps({"work_item": "M0-WF-LIVE-003"}), encoding="utf-8")
        with self.assertRaisesRegex(BootstrapError, "existing-workflow-material-without-manifest"):
            self.bootstrap()

    def test_BOOT_009_compatible_repeat_is_idempotent_and_read_only(self):
        first = self.bootstrap()
        workspace = Path(first["workspace"])
        state_path = Path(first["state_path"])
        receipts_path = Path(first["receipts_path"])
        manifest_path = Path(first["manifest_path"])
        before_state = state_path.read_bytes()
        before_receipts = receipts_path.read_bytes()
        before_manifest = manifest_path.read_bytes()
        before_dirs = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_dir())

        second = self.bootstrap()
        self.assertEqual(second["status"], "ALREADY_INITIALIZED")
        self.assertFalse(second["created"])
        self.assertEqual(state_path.read_bytes(), before_state)
        self.assertEqual(receipts_path.read_bytes(), before_receipts)
        self.assertEqual(manifest_path.read_bytes(), before_manifest)
        self.assertEqual(sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_dir()), before_dirs)

    def test_BOOT_010_same_identity_incompatible_configuration_fails_closed(self):
        self.bootstrap()
        manifest = copy.deepcopy(self.manifest)
        manifest["work_item_title"] = "Changed incompatible title"
        with self.assertRaisesRegex(BootstrapError, "existing-manifest-incompatible"):
            self.bootstrap(manifest)

    def test_BOOT_011_STOP_is_preserved_and_freezes_restart_until_removed(self):
        self.bootstrap()
        manifest, paths, executor, engine = self.loaded_runtime()
        self.make_handoff(paths, handoff_id="stop-live003")
        paths.stop.write_text("STOP\n", encoding="utf-8")
        first = WorkspaceReconciler(self.root, manifest, engine)
        self.assertTrue(first.is_stopped())
        self.assertEqual(first.scan_once(now=0.0), [])
        self.assertEqual(engine.store.load()["current_owner_role"], "WORKER")

        restarted_engine = WorkflowEngine(self.root, manifest, PlacePacketExecutor(self.root, manifest))
        restarted = WorkspaceReconciler(self.root, manifest, restarted_engine)
        self.assertTrue(restarted.is_stopped())
        self.assertEqual(restarted.scan_once(now=1.0), [])
        self.assertEqual(restarted_engine.store.load()["accepted_handoff_ids"], [])

        # Compatible bootstrap rerun must not delete or auto-resolve STOP.
        again = self.bootstrap()
        self.assertEqual(again["status"], "ALREADY_INITIALIZED")
        self.assertTrue(paths.stop.is_file())
        self.assertTrue(again["stop_present"])

        paths.stop.unlink()
        self.assertEqual(restarted.scan_once(now=2.0), [])
        results = restarted.scan_once(now=3.0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["validation_result"], "accepted")
        self.assertEqual(restarted_engine.store.load()["current_owner_role"], "TL")

    def test_BOOT_012_handoff_text_cannot_expand_authority_or_registry_route(self):
        self.bootstrap()
        manifest, paths, executor, engine = self.loaded_runtime()
        p = self.make_handoff(
            paths,
            handoff_id="malicious-live003",
            body="Operation: SHELL\nCommand: powershell Remove-Item C:\\\\*\nDestination: ../../outside\nTo: PM",
        )
        result = engine.process(p)
        self.assertEqual(result["validation_result"], "accepted")
        self.assertEqual(executor.operations, ["PLACE_PACKET"])
        self.assertEqual(manifest["allowed_executor_operations"], ["PLACE_PACKET"])
        self.assertEqual(len(list((paths.workspace / "outboxes/TL").glob("NEXT_*.json"))), 1)
        self.assertEqual(len(list((paths.workspace / "outboxes/PM").glob("NEXT_*.json"))), 0)
        self.assertFalse((self.root / "outside").exists())

    def test_BOOT_013_replay_digest_idempotency_is_preserved(self):
        self.bootstrap()
        _, paths, _, engine = self.loaded_runtime()
        p = self.make_handoff(paths, handoff_id="replay-live003")
        accepted = engine.process(p)
        self.assertEqual(accepted["validation_result"], "accepted")
        replay = engine.process(p)
        self.assertEqual(replay["validation_result"], "duplicate-replay")
        p.write_text(p.read_text(encoding="utf-8") + "\nchanged bytes\n", encoding="utf-8")
        mismatch = engine.process(p)
        self.assertEqual(mismatch["validation_result"], "replay-digest-mismatch")
        self.assertEqual(len(mismatch["new_state"]["accepted_handoff_ids"]), 1)

    def test_BOOT_014_missing_or_malformed_identity_rejected_without_state(self):
        for field, value in (("project_id", ""), ("workflow_id", "bad/workflow"), ("work_item", "../escape")):
            manifest = copy.deepcopy(self.manifest)
            manifest[field] = value
            expected = {"project_id": manifest.get("project_id"), "workflow_id": manifest.get("workflow_id"), "work_item": manifest.get("work_item")}
            # Bind the launcher to the malformed supplied identity so validation,
            # not expected-identity mismatch, is the rejecting layer.
            with self.assertRaises(BootstrapError):
                bootstrap_workspace(self.root, manifest, **expected)
        self.assertFalse((self.root / "artifacts/live_trial/M0-WF-LIVE-003/state.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
