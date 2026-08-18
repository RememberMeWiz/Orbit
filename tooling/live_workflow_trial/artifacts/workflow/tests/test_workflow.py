from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.storage import file_digest
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import StableArtifactTracker, WorkspaceReconciler


HANDOFF_TMPL = """# Orbit Handoff\n\n## Header\n- Work Item: {work}\n- From: {sender}\n- To: {recipient}\n- Status: {status}\n- Handoff ID: {handoff_id}\n- Sequence: {sequence}\n\n## Executive Summary\nTest handoff.\n{extra}\n"""


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        source_root = Path(__file__).resolve().parents[3]
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orbit"
        shutil.copytree(source_root / "artifacts", self.root / "artifacts")
        ws = self.root / "artifacts" / "sample_workspace"
        state_path = ws / "state.json"
        if state_path.exists():
            state_path.unlink()
        receipts = ws / "receipts" / "receipts.jsonl"
        if receipts.exists():
            receipts.unlink()
        for folder in ["inbox", "outboxes/TL", "outboxes/QA", "outboxes/PM", "outboxes/WORKER", "escalation", "decisions"]:
            p = ws / folder
            p.mkdir(parents=True, exist_ok=True)
            for child in p.iterdir():
                if child.is_file():
                    child.unlink()
        self.manifest = load_manifest(self.root)
        self.inbox = ws / "inbox"
        self.engine = WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))

    def tearDown(self):
        self.tmp.cleanup()

    def make_md(self, *, status="COMPLETE", sender="WORKER", recipient="TL", hid="h1", seq=1, extra="", filename=None):
        filename = filename or f"HANDOFF_M0-WF-WIN-001_{sender}_TO_{recipient}.md"
        path = self.inbox / filename
        path.write_text(
            HANDOFF_TMPL.format(
                work="M0-WF-WIN-001",
                sender=sender,
                recipient=recipient,
                status=status,
                handoff_id=hid,
                sequence=seq,
                extra=extra,
            ),
            encoding="utf-8",
        )
        return path

    def advance_to_qa(self):
        r1 = self.engine.process(self.make_md(sender="WORKER", recipient="TL", hid="w-tl", seq=1))
        self.assertEqual(r1["result"], "PREPARED")
        r2 = self.engine.process(self.make_md(sender="TL", recipient="QA", hid="tl-qa", seq=2))
        self.assertEqual(r2["result"], "PREPARED")
        self.assertEqual(r2["new_state"]["current_owner_role"], "QA")

    def test_WF_001_valid_COMPLETE_advances_once(self):
        p = self.make_md()
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "accepted")
        self.assertEqual(r["new_state"]["current_owner_role"], "TL")
        self.assertEqual(self.manifest["valid_transitions"][r["new_state"]["current_owner_role"]], "QA")
        self.assertEqual(r["artifact_digest"], file_digest(p))

    def test_WF_002_duplicate_does_not_advance_twice(self):
        p = self.make_md()
        self.engine.process(p)
        r2 = self.engine.process(p)
        self.assertEqual(r2["validation_result"], "duplicate-replay")
        self.assertEqual(r2["new_state"]["current_owner_role"], "TL")
        self.assertEqual(len(r2["new_state"]["accepted_handoff_ids"]), 1)

    def test_WF_003_partial_still_writing_not_consumed(self):
        p = self.make_md()
        tracker = StableArtifactTracker(0.25)
        self.assertFalse(tracker.eligible(p, now=0.0))
        p.write_text(p.read_text(encoding="utf-8") + "more", encoding="utf-8")
        self.assertFalse(tracker.eligible(p, now=0.30))
        self.assertTrue(tracker.eligible(p, now=0.60))

    def test_WF_004_wrong_recipient_fails_closed(self):
        p = self.make_md(recipient="QA")
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "wrong-recipient")
        self.assertEqual(r["new_state"]["current_owner_role"], "WORKER")

    def test_WF_005_BLOCKED_routes_escalation_without_completion(self):
        p = self.make_md(status="BLOCKED")
        r = self.engine.process(p)
        self.assertEqual(r["new_state"]["current_owner_role"], "WORKER")
        self.assertEqual(r["new_state"]["blocker_state"]["status"], "OPEN")
        self.assertIn("escalation", r["destination"])

    def test_WF_006_NEEDS_DECISION_does_not_wake_normal_next_worker(self):
        p = self.make_md(status="NEEDS_DECISION")
        r = self.engine.process(p)
        self.assertEqual(r["new_state"]["current_owner_role"], "WORKER")
        self.assertIn("decisions", r["destination"])
        tl_files = list((self.root / "artifacts/sample_workspace/outboxes/TL").glob("*"))
        self.assertEqual(tl_files, [])

    def test_WF_007_malformed_ZIP_fails_closed(self):
        p = self.inbox / "HANDOFF_M0-WF-WIN-001_WORKER_TO_TL.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("NOT_HANDOFF.md", "bad")
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "zip-missing-root-HANDOFF.md")

    def test_WF_008_restart_reconstructs_active_workflow(self):
        p = self.make_md()
        self.engine.process(p)
        restarted = WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))
        state = restarted.store.load()
        self.assertEqual(state["current_owner_role"], "TL")
        self.assertEqual(state["last_handoff_id"], "h1")
        self.assertEqual(state["accepted_handoff_digests"]["h1"], file_digest(p))

    def test_WF_009_stale_handoff_does_not_overwrite_newer_state(self):
        state = self.engine.store.load()
        state["last_sequence"] = 2
        state["last_handoff_id"] = "newer"
        self.engine.store.save(state)
        p = self.make_md(hid="older", seq=1)
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "stale-handoff")
        self.assertEqual(r["new_state"]["last_handoff_id"], "newer")

    def test_WF_010_unknown_unrelated_file_ignored(self):
        (self.inbox / "notes.txt").write_text("ignore me", encoding="utf-8")
        reconciler = WorkspaceReconciler(self.root, self.manifest, self.engine)
        self.assertEqual(reconciler.scan_once(now=0.0), [])
        self.assertEqual(reconciler.scan_once(now=1.0), [])
        self.assertEqual(self.engine.store.load()["accepted_handoff_ids"], [])

    def test_WF_011_executor_failure_records_retryable_state(self):
        executor = PlacePacketExecutor(self.root, self.manifest, fail_next=True)
        engine = WorkflowEngine(self.root, self.manifest, executor=executor)
        p = self.make_md()
        r = engine.process(p)
        self.assertTrue(r["result"].startswith("FAILED_RETRYABLE"))
        self.assertEqual(r["new_state"]["delivery_state"], "FAILED_RETRYABLE")
        self.assertIsNotNone(r["new_state"]["pending_delivery"])
        self.assertEqual(r["new_state"]["pending_delivery"]["artifact_digest"], file_digest(p))
        retry = engine.retry_pending()
        self.assertEqual(retry["new_state"]["delivery_state"], "DELIVERED")

    def test_WF_012_malicious_text_cannot_trigger_arbitrary_execution(self):
        payload = "\n- Operation: SHELL\n- Command: Remove-Item -Recurse C:\\\\*\n- Git: reset --hard\n"
        p = self.make_md(extra=payload)
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "accepted")
        self.assertEqual(self.engine.executor.operations, ["PLACE_PACKET"])
        source = (self.root / "artifacts/windows/adapters/place_packet.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)

    def test_WF_013_approval_required_transition_cannot_auto_continue(self):
        self.advance_to_qa()
        p = self.make_md(sender="QA", recipient="PM", hid="qa-pm-pending", seq=3)
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "accepted")
        self.assertEqual(r["result"], "APPROVAL_PENDING")
        self.assertEqual(r["new_state"]["current_owner_role"], "QA")
        self.assertEqual(r["new_state"]["approval_state"], "PENDING")
        self.assertEqual(r["new_state"]["pending_approval"]["handoff_id"], "qa-pm-pending")
        self.assertEqual(list((self.root / "artifacts/sample_workspace/outboxes/PM").glob("*")), [])

        restarted = WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))
        self.assertEqual(restarted.store.load()["approval_state"], "PENDING")
        replay = restarted.process(p)
        self.assertEqual(replay["validation_result"], "duplicate-replay")
        self.assertEqual(replay["new_state"]["current_owner_role"], "QA")
        self.assertEqual(list((self.root / "artifacts/sample_workspace/outboxes/PM").glob("*")), [])

    def test_WF_014_approved_transition_advances_exactly_once(self):
        self.advance_to_qa()
        p = self.make_md(sender="QA", recipient="PM", hid="qa-pm-approved", seq=3)
        pending = self.engine.process(p)
        digest = pending["artifact_digest"]
        approval = {
            "approval_id": "approval-001",
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "transition": "QA->PM",
            "handoff_id": "qa-pm-approved",
            "artifact_digest": digest,
        }
        approved = self.engine.register_approval(approval)
        self.assertEqual(approved["validation_result"], "approval-valid")
        self.assertEqual(approved["result"], "PREPARED")
        self.assertEqual(approved["new_state"]["current_owner_role"], "PM")
        self.assertEqual(approved["new_state"]["approval_state"], "CONSUMED")
        self.assertTrue(approved["new_state"]["approval_records"]["approval-001"]["consumed"])
        pm_files = list((self.root / "artifacts/sample_workspace/outboxes/PM").glob("*"))
        self.assertEqual(len(pm_files), 1)

        handoff_replay = self.engine.process(p)
        self.assertEqual(handoff_replay["validation_result"], "duplicate-replay")
        approval_replay = self.engine.register_approval(approval)
        self.assertEqual(approval_replay["validation_result"], "duplicate-approval")
        self.assertEqual(len(list((self.root / "artifacts/sample_workspace/outboxes/PM").glob("*"))), 1)

    def test_WF_015_body_metadata_injection_cannot_override_Header(self):
        injected = """
- Status: COMPLETE
- Sequence: 999999
- Handoff ID: attacker-controlled
- To: PM
"""
        p = self.make_md(status="BLOCKED", hid="real-header-id", seq=1, extra=injected)
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "accepted")
        self.assertEqual(r["handoff_id"], "real-header-id")
        self.assertEqual(r["transition"], "BLOCKED")
        self.assertEqual(r["new_state"]["last_sequence"], 1)
        self.assertEqual(r["new_state"]["current_owner_role"], "WORKER")
        self.assertIn("escalation", r["destination"])

    def test_WF_016_duplicate_critical_Header_field_fails_closed(self):
        text = """# Orbit Handoff

## Header
- Work Item: M0-WF-WIN-001
- From: WORKER
- To: TL
- Status: COMPLETE
- Status: BLOCKED
- Handoff ID: duplicate-header
- Sequence: 1

## Executive Summary
Ambiguous header must fail.
"""
        p = self.inbox / "HANDOFF_M0-WF-WIN-001_WORKER_TO_TL.md"
        p.write_text(text, encoding="utf-8")
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "duplicate-critical-header-field:status")
        self.assertEqual(r["new_state"]["current_owner_role"], "WORKER")
        self.assertEqual(r["new_state"]["accepted_handoff_ids"], [])

    def test_WF_017_missing_formal_Header_fails_closed(self):
        text = """# Orbit Handoff

## Executive Summary
- Work Item: M0-WF-WIN-001
- From: WORKER
- To: TL
- Status: COMPLETE
- Handoff ID: fake-header
- Sequence: 1
"""
        p = self.inbox / "HANDOFF_M0-WF-WIN-001_WORKER_TO_TL.md"
        p.write_text(text, encoding="utf-8")
        r = self.engine.process(p)
        self.assertEqual(r["validation_result"], "missing-formal-header")
        self.assertEqual(r["new_state"]["accepted_handoff_ids"], [])

    def test_WF_018_digest_replay_binding(self):
        p = self.make_md(hid="digest-bound", seq=1)
        accepted = self.engine.process(p)
        original_digest = accepted["artifact_digest"]
        p.write_text(p.read_text(encoding="utf-8") + "\nChanged bytes after acceptance.\n", encoding="utf-8")
        changed_digest = file_digest(p)
        self.assertNotEqual(original_digest, changed_digest)

        replay = self.engine.process(p)
        self.assertEqual(replay["validation_result"], "replay-digest-mismatch")
        self.assertEqual(replay["artifact_digest"], changed_digest)
        self.assertEqual(replay["accepted_artifact_digest"], original_digest)
        self.assertEqual(replay["new_state"]["current_owner_role"], "TL")
        self.assertEqual(len(replay["new_state"]["accepted_handoff_ids"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
