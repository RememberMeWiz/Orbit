from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import _winapi  # CPython Windows-only junction helper
except ImportError:  # pragma: no cover - native suite is skipped off Windows
    _winapi = None

from workflow.core.bootstrap import BootstrapError, bootstrap_workspace
from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.runtime import RuntimeConfigurationError
from workflow.core.storage import file_digest
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler
from windows.qa_observability import TRACE_CANARIES, read_receipts, scan_canaries, write_gate_evidence


HANDOFF_TMPL = """# Orbit Handoff\n\n## Header\n- Work Item: {work}\n- From: {sender}\n- To: {recipient}\n- Status: {status}\n- Handoff ID: {handoff_id}\n- Sequence: {sequence}\n\n## Executive Summary\nNative Windows validation fixture.\n{extra}\n"""


@unittest.skipUnless(os.name == "nt", "native Windows gate requires os.name == 'nt'")
class NativeWindowsGateTests(unittest.TestCase):
    def setUp(self):
        self.source_root = Path(__file__).resolve().parents[3]
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = self.fresh_runtime("main")
        self.root = self.runtime["root"]
        self.manifest = self.runtime["manifest"]
        self.inbox = self.runtime["inbox"]
        self.executor = self.runtime["executor"]
        self.engine = self.runtime["engine"]

    def tearDown(self):
        self.tmp.cleanup()

    def fresh_runtime(self, name: str, manifest_mutator=None):
        root = Path(self.tmp.name) / name
        shutil.copytree(self.source_root / "artifacts", root / "artifacts")
        ws = root / "artifacts/sample_workspace"
        for f in [ws / "state.json", ws / "receipts/receipts.jsonl"]:
            if f.exists():
                f.unlink()
        for rel in ["inbox", "outboxes/WORKER", "outboxes/TL", "outboxes/QA", "outboxes/PM", "escalation", "decisions"]:
            p = ws / rel
            p.mkdir(parents=True, exist_ok=True)
            for child in p.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
        manifest = load_manifest(root)
        if manifest_mutator is not None:
            manifest_mutator(manifest)
        inbox = ws / "inbox"
        executor = PlacePacketExecutor(root, manifest)
        engine = WorkflowEngine(root, manifest, executor)
        return {"root": root, "manifest": manifest, "inbox": inbox, "executor": executor, "engine": engine}

    def make_md(self, runtime=None, sender="WORKER", recipient="TL", status="COMPLETE", hid="native-1", seq=1, extra="", work=None):
        runtime = runtime or self.runtime
        work = work or runtime["manifest"]["work_item"]
        p = runtime["inbox"] / f"HANDOFF_{work}_{sender}_TO_{recipient}.md"
        p.write_text(
            HANDOFF_TMPL.format(work=work, sender=sender, recipient=recipient, status=status, handoff_id=hid, sequence=seq, extra=extra),
            encoding="utf-8",
        )
        return p

    def advance_to_qa(self, runtime=None):
        runtime = runtime or self.runtime
        runtime["engine"].process(self.make_md(runtime, "WORKER", "TL", hid="w-tl", seq=1))
        runtime["engine"].process(self.make_md(runtime, "TL", "QA", hid="tl-qa", seq=2))

    @staticmethod
    def runtime_observability(runtime, result=None):
        receipts_path = runtime["root"] / "artifacts/sample_workspace/receipts/receipts.jsonl"
        receipts = read_receipts(receipts_path)
        state = runtime["engine"].store.load()
        payload = {
            "resolved_project_id": runtime["manifest"].get("project_id"),
            "resolved_workflow_id": runtime["manifest"].get("workflow_id"),
            "resolved_work_item": runtime["manifest"].get("work_item"),
            "state_revision": state.get("state_revision"),
            "work_state": state.get("work_state"),
            "delivery_state": state.get("delivery_state"),
            "approval_state": state.get("approval_state"),
            "last_handoff_id": state.get("last_handoff_id"),
            "last_artifact_digest": state.get("last_artifact_digest"),
            "receipt_count": len(receipts),
            "receipt_ids": [r.get("receipt_id") for r in receipts],
            "last_path_decision": runtime["executor"].last_path_decision,
            "executor_operations": list(runtime["executor"].operations),
        }
        if result:
            payload.update({
                "validation_decision": result.get("validation_decision"),
                "validation_result": result.get("validation_result"),
                "reason_code": result.get("reason_code", result.get("validation_result")),
                "route_decision_id": result.get("route_decision_id"),
                "executor_idempotency_key": result.get("executor_idempotency_key"),
                "state_revision_before": result.get("state_revision_before", (result.get("old_state") or {}).get("state_revision")),
                "state_revision_after": result.get("state_revision_after", (result.get("new_state") or {}).get("state_revision")),
                "approval_consumption_state": result.get("approval_consumption_state", (result.get("new_state") or {}).get("approval_state")),
            })
        return payload

    def test_NWIN_001_native_runtime_and_windows_path_semantics(self):
        self.assertEqual(os.name, "nt")
        self.assertTrue(Path("C:\\").is_absolute())
        self.assertEqual(Path("C:\\temp").drive.upper(), "C:")
        write_gate_evidence("NWIN-001", {
            "status": "PASS",
            "os_name": os.name,
            "windows_absolute_path_semantics": True,
            "configured_watched_root": str(self.inbox),
            "configured_packet_root": str(self.root / "artifacts/sample_workspace/outboxes"),
        })

    def test_NWIN_002_reconciler_processes_disposable_workspace(self):
        before = self.engine.store.load()
        p = self.make_md(hid="reconcile-native")
        digest = file_digest(p)
        initial_tree = sorted(str(x.relative_to(self.root)) for x in self.root.rglob("*") if x.is_file())
        reconciler = WorkspaceReconciler(self.root, self.manifest, self.engine)
        self.assertEqual(reconciler.scan_once(now=0.0), [])
        results = reconciler.scan_once(now=1.0)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["validation_result"], "accepted")
        self.assertEqual(result["new_state"]["current_owner_role"], "TL")
        self.assertEqual(result["artifact_digest"], digest)
        packets = list((self.root / "artifacts/sample_workspace/outboxes/TL").glob("NEXT_*.json"))
        self.assertEqual(len(packets), 1)
        packet = json.loads(packets[0].read_text(encoding="utf-8"))
        receipts = read_receipts(self.root / "artifacts/sample_workspace/receipts/receipts.jsonl")
        write_gate_evidence("NWIN-002", {
            "status": "PASS",
            "initial_file_tree": initial_tree,
            "input_handoff_sha256": digest,
            "state_before": before,
            "stability_observation": result.get("stability_observation"),
            "validation_decision": result.get("validation_decision"),
            "reason_code": result.get("reason_code", result.get("validation_result")),
            "route_decision_id": packet.get("route_decision_id"),
            "executor_idempotency_key": packet.get("idempotency_key"),
            "receipt_identity": receipts[-1].get("receipt_id"),
            "receipt_count": len(receipts),
            "state_after": result["new_state"],
            "packet_destination": str(packets[0]),
            "windows_path_decision": self.executor.last_path_decision,
        })

    def test_NWIN_003_create_write_rename_and_duplicate_notifications_are_stable(self):
        temp_name = self.inbox / "handoff.partial"
        temp_name.write_text("partial", encoding="utf-8")
        reconciler = WorkspaceReconciler(self.root, self.manifest, self.engine)
        self.assertEqual(reconciler.scan_once(now=0.0), [])
        final = self.inbox / "HANDOFF_M0-WF-WIN-001_WORKER_TO_TL.md"
        temp_name.write_text(HANDOFF_TMPL.format(work="M0-WF-WIN-001", sender="WORKER", recipient="TL", status="COMPLETE", handoff_id="rename-native", sequence=1, extra=""), encoding="utf-8")
        temp_name.replace(final)
        self.assertEqual(reconciler.scan_once(now=0.1), [])
        results = reconciler.scan_once(now=0.5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["handoff_id"], "rename-native")
        self.assertEqual(reconciler.scan_once(now=0.8), [])
        obs = reconciler.tracker.observation(final)
        self.assertEqual(obs["processing_count"], 1)

        repeated = self.fresh_runtime("stable-repeated")
        p2 = self.make_md(repeated, hid="repeated-write")
        r2 = WorkspaceReconciler(repeated["root"], repeated["manifest"], repeated["engine"])
        self.assertEqual(r2.scan_once(now=0.0), [])
        p2.write_text(p2.read_text(encoding="utf-8") + "\nappend-1", encoding="utf-8")
        self.assertEqual(r2.scan_once(now=0.20), [])
        p2.write_text(p2.read_text(encoding="utf-8") + "\nappend-2", encoding="utf-8")
        self.assertEqual(r2.scan_once(now=0.40), [])
        self.assertEqual(r2.scan_once(now=0.55), [])
        final_results = r2.scan_once(now=0.70)
        self.assertEqual(len(final_results), 1)

        missed = self.fresh_runtime("stable-missed-event")
        p3 = self.make_md(missed, hid="missed-event")
        r3 = WorkspaceReconciler(missed["root"], missed["manifest"], missed["engine"])
        self.assertEqual(r3.scan_once(now=10.0), [])
        missed_results = r3.scan_once(now=11.0)
        self.assertEqual(len(missed_results), 1)

        write_gate_evidence("NWIN-003", {
            "status": "PASS",
            "create_write_rename": {
                **obs,
                "content_hash_after_stability": file_digest(final),
                "processing_count": obs["processing_count"],
            },
            "repeated_write_extends_window": {
                **r2.tracker.observation(p2),
                "content_hash_after_stability": file_digest(p2),
            },
            "missed_notification_reconciled": {
                **r3.tracker.observation(p3),
                "content_hash_after_stability": file_digest(p3),
            },
            "duplicate_notification_second_processing_count": 0,
        })

    def test_NWIN_004_restart_reconstructs_pending_delivery_and_approval(self):
        retry_rt = self.fresh_runtime("restart-retry")
        retry_rt["executor"].fail_next = True
        p = self.make_md(retry_rt, hid="retry-native")
        first = retry_rt["engine"].process(p)
        self.assertEqual(first["new_state"]["delivery_state"], "FAILED_RETRYABLE")
        pre = first["new_state"]
        restarted = WorkflowEngine(retry_rt["root"], retry_rt["manifest"], PlacePacketExecutor(retry_rt["root"], retry_rt["manifest"]))
        recovered = restarted.store.load()
        self.assertEqual(recovered["last_handoff_id"], "retry-native")
        self.assertIsNotNone(recovered["pending_delivery"])
        retried = restarted.retry_pending()
        self.assertIsNotNone(retried)
        self.assertEqual(retried["new_state"]["delivery_state"], "DELIVERED")

        approval_rt = self.fresh_runtime("restart-approval")
        self.advance_to_qa(approval_rt)
        pending = approval_rt["engine"].process(self.make_md(approval_rt, "QA", "PM", hid="restart-approval", seq=3))
        self.assertEqual(pending["result"], "APPROVAL_PENDING")
        approval_restart = WorkflowEngine(approval_rt["root"], approval_rt["manifest"], PlacePacketExecutor(approval_rt["root"], approval_rt["manifest"]))
        approval_state = approval_restart.store.load()
        self.assertEqual(approval_state["approval_state"], "PENDING")
        self.assertEqual(approval_state["pending_approval"]["handoff_id"], "restart-approval")

        retry_receipts = read_receipts(retry_rt["root"] / "artifacts/sample_workspace/receipts/receipts.jsonl")
        write_gate_evidence("NWIN-004", {
            "status": "PASS",
            "pre_restart_state_revision": pre["state_revision"],
            "pre_restart_work_state": pre["work_state"],
            "pre_restart_delivery_state": pre["delivery_state"],
            "pre_restart_approval_state": pre["approval_state"],
            "last_handoff_id": pre["last_handoff_id"],
            "last_artifact_digest": pre["last_artifact_digest"],
            "pending_delivery": pre["pending_delivery"] is not None,
            "post_restart_state_revision": recovered["state_revision"],
            "recovered_pending_work": recovered["pending_delivery"] is not None,
            "executor_idempotency_identity": recovered["pending_delivery"].get("idempotency_key"),
            "receipt_count_after_retry": len(retry_receipts),
            "approval_restart": {
                "approval_state": approval_state["approval_state"],
                "pending_approval": approval_state["pending_approval"] is not None,
                "state_revision": approval_state["state_revision"],
            },
        })

    def test_NWIN_005_windows_path_safety_and_canonicalization(self):
        normal = self.fresh_runtime("path-normal")
        packet = {"handoff_id": "path-normal", "artifact_digest": "0" * 64, "to": "TL"}
        ok, result, _ = normal["executor"].place_packet("TL", packet)
        self.assertTrue(ok)
        self.assertEqual(result, "PREPARED")
        self.assertEqual(normal["executor"].last_path_decision["decision"], "ALLOW")

        decisions = {"normal": normal["executor"].last_path_decision}
        bad_values = {
            "parent_traversal": "..\\escape",
            "absolute_drive": "C:\\orbit-outside",
            "unc": "\\\\server\\share\\orbit-outside",
            "drive_root": "C:\\",
        }
        invalid_config_results = {}
        for label, endpoint in bad_values.items():
            def mutate(manifest, endpoint=endpoint):
                manifest["destinations"]["TL"] = endpoint
                manifest["role_destination_registry"]["TL"]["endpoint_ref"] = endpoint

            invalid_root = Path(self.tmp.name) / f"path-{label}"
            with self.assertRaises(RuntimeConfigurationError) as raised:
                self.fresh_runtime(f"path-{label}", mutate)

            # LIVE-002 resolves and validates trusted runtime paths during
            # WorkflowEngine initialization. Invalid destination configuration
            # must fail before any PLACE_PACKET call or packet write occurs.
            packet_writes = list(invalid_root.rglob("NEXT_*.json")) if invalid_root.exists() else []
            self.assertEqual(packet_writes, [])
            invalid_config_results[label] = {
                "exception_type": type(raised.exception).__name__,
                "exception": str(raised.exception),
                "packet_write_count": len(packet_writes),
            }

        # Windows separator/case alias remains inside the configured root.
        def alias_mutate(manifest):
            alias = "sample_workspace\\outboxes\\TL"
            manifest["destinations"]["TL"] = alias
            manifest["role_destination_registry"]["TL"]["endpoint_ref"] = alias
        alias_rt = self.fresh_runtime("path-alias", alias_mutate)
        ok, _, _ = alias_rt["executor"].place_packet("TL", {"handoff_id": "path-alias", "artifact_digest": "2" * 64, "to": "TL"})
        self.assertTrue(ok)
        decisions["alternate_separator"] = alias_rt["executor"].last_path_decision

        # Reparse-point escape proof using a directory junction rather than a
        # symbolic link. Junction creation must work for the normal non-elevated
        # Windows validation path and must not depend on SeCreateSymbolicLinkPrivilege.
        reparse_rt = self.fresh_runtime("path-reparse")
        outbox = reparse_rt["root"] / "artifacts/sample_workspace/outboxes/TL"
        shutil.rmtree(outbox)
        outside = Path(self.tmp.name) / "outside-reparse"
        outside.mkdir(parents=True, exist_ok=True)
        self.assertIsNotNone(_winapi, "NWIN-005 requires CPython's Windows junction helper")
        try:
            _winapi.CreateJunction(str(outside), str(outbox))
            self.assertTrue(outbox.is_junction(), "NWIN-005 fixture must be a directory junction")
            ok, fail_result, _ = reparse_rt["executor"].place_packet(
                "TL",
                {"handoff_id": "path-reparse", "artifact_digest": "3" * 64, "to": "TL"},
            )
            self.assertFalse(ok)
            self.assertIn("reparse", fail_result)
            decisions["reparse_escape"] = reparse_rt["executor"].last_path_decision
            self.assertTrue(decisions["reparse_escape"]["reparse_point_encountered"])
            self.assertEqual(len(list(outside.rglob("*"))), 0)
        finally:
            # Remove the junction itself, never the target directory.
            if outbox.exists() or outbox.is_junction():
                outbox.rmdir()

        write_gate_evidence("NWIN-005", {
            "status": "PASS",
            "invalid_destination_configuration": invalid_config_results,
            "invalid_configuration_fails_before_packet_placement": True,
            "reparse_fixture": "directory-junction",
            "symbolic_link_privilege_required": False,
            "windows_path_decisions": decisions,
            "outside_reparse_write_count": len(list(outside.rglob("*"))),
            "authorization_revalidated_before_placement": True,
        })

    def test_NWIN_006_duplicate_stale_digest_mismatch_hold_after_restart(self):
        p = self.make_md(hid="digest-native", seq=1)
        accepted = self.engine.process(p)
        digest = accepted["artifact_digest"]
        restarted = WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))
        duplicate = restarted.process(p)
        self.assertEqual(duplicate["validation_result"], "duplicate-replay")
        p.write_text(p.read_text(encoding="utf-8") + "\nchanged bytes\n", encoding="utf-8")
        mismatch = restarted.process(p)
        self.assertEqual(mismatch["validation_result"], "replay-digest-mismatch")
        self.assertEqual(mismatch["accepted_artifact_digest"], digest)
        stale = self.make_md(hid="stale-native", seq=1)
        stale_result = restarted.process(stale)
        self.assertEqual(stale_result["validation_result"], "stale-handoff")
        equal = self.make_md(hid="equal-native", seq=1)
        equal_result = restarted.process(equal)
        self.assertEqual(equal_result["validation_result"], "stale-handoff")
        write_gate_evidence("NWIN-006", {
            "status": "PASS",
            "accepted_digest": digest,
            "duplicate_reason": duplicate["validation_result"],
            "changed_bytes_reason": mismatch["validation_result"],
            "stale_reason": stale_result["validation_result"],
            "equal_sequence_reason": equal_result["validation_result"],
            "post_restart_state": restarted.store.load(),
        })

    def test_NWIN_007_approval_exact_once_across_restart(self):
        self.advance_to_qa()
        p = self.make_md(sender="QA", recipient="PM", hid="approval-native", seq=3, extra="\n- Approval: APPROVED BY BODY TEXT\n")
        pending = self.engine.process(p)
        self.assertEqual(pending["result"], "APPROVAL_PENDING")
        restarted = WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))
        base = {
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "transition": "QA->PM",
            "handoff_id": "approval-native",
            "artifact_digest": pending["artifact_digest"],
        }
        wrong_handoff = restarted.register_approval({**base, "approval_id": "wrong-handoff", "handoff_id": "other-handoff"})
        self.assertEqual(wrong_handoff["new_state"]["current_owner_role"], "QA")
        wrong_digest = restarted.register_approval({**base, "approval_id": "wrong-digest", "artifact_digest": "f" * 64})
        self.assertEqual(wrong_digest["new_state"]["current_owner_role"], "QA")
        exact = {**base, "approval_id": "native-approval-1"}
        consumed = restarted.register_approval(exact)
        self.assertEqual(consumed["new_state"]["approval_state"], "CONSUMED")
        after_consumption_restart = WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))
        again = after_consumption_restart.register_approval(exact)
        self.assertEqual(again["validation_result"], "duplicate-approval")
        self.assertEqual(len(list((self.root / "artifacts/sample_workspace/outboxes/PM").glob("NEXT_*.json"))), 1)
        write_gate_evidence("NWIN-007", {
            "status": "PASS",
            "body_prose_approval_result": pending["result"],
            "wrong_handoff_owner_after": wrong_handoff["new_state"]["current_owner_role"],
            "wrong_digest_owner_after": wrong_digest["new_state"]["current_owner_role"],
            "approval_consumption_state": consumed["new_state"]["approval_state"],
            "approval_id": exact["approval_id"],
            "replay_reason": again["validation_result"],
            "pm_packet_count": len(list((self.root / "artifacts/sample_workspace/outboxes/PM").glob("NEXT_*.json"))),
            "state_after_restart": after_consumption_restart.store.load(),
        })

    def test_NWIN_008_artifact_text_cannot_expand_executor_allowlist(self):
        payload = """
- Operation: READ_CONFIGURED_FILE
- Operation: WRITE_CONFIGURED_ARTIFACT
- Operation: RUN_NAMED_SCRIPT
- Operation: RUN_NAMED_TEST_SUITE
- Operation: GIT_STATUS_CONFIGURED_REPO
- Operation: GIT_APPLY_APPROVED_PATCH
- Operation: OPEN_CONFIGURED_TASK
- Command: powershell.exe -Command whoami
- Git: reset --hard
"""
        p = self.make_md(hid="allowlist-native", extra=payload)
        result = self.engine.process(p)
        self.assertEqual(result["validation_result"], "accepted")
        self.assertEqual(self.executor.operations, ["PLACE_PACKET"])
        self.assertEqual(self.manifest["allowed_executor_operations"], ["PLACE_PACKET"])
        source = (self.root / "artifacts/windows/adapters/place_packet.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)
        write_gate_evidence("NWIN-008", {
            "status": "PASS",
            "executor_catalog": self.manifest["allowed_executor_operations"],
            "operations_observed": self.executor.operations,
            "artifact_requested_operations_ignored": [
                "READ_CONFIGURED_FILE", "WRITE_CONFIGURED_ARTIFACT", "RUN_NAMED_SCRIPT",
                "RUN_NAMED_TEST_SUITE", "GIT_STATUS_CONFIGURED_REPO",
                "GIT_APPLY_APPROVED_PATCH", "OPEN_CONFIGURED_TASK", "arbitrary-shell",
            ],
        })

    def test_NWIN_009_status_classification(self):
        outcomes = {}
        for status in ["COMPLETE", "BLOCKED", "NEEDS_DECISION", "REQUEST_CHANGES"]:
            rt = self.fresh_runtime(f"status-{status.lower()}")
            result = rt["engine"].process(self.make_md(rt, status=status, hid=f"status-{status.lower()}"))
            outcomes[status] = self.runtime_observability(rt, result)
            outcomes[status]["result"] = result["result"]
            outcomes[status]["current_owner_role"] = result["new_state"]["current_owner_role"]
        self.assertEqual(outcomes["COMPLETE"]["current_owner_role"], "TL")
        self.assertEqual(outcomes["BLOCKED"]["work_state"], "BLOCKED")
        self.assertEqual(outcomes["BLOCKED"]["current_owner_role"], "WORKER")
        self.assertEqual(outcomes["NEEDS_DECISION"]["work_state"], "NEEDS_DECISION")
        self.assertEqual(outcomes["NEEDS_DECISION"]["current_owner_role"], "WORKER")
        self.assertEqual(outcomes["REQUEST_CHANGES"]["work_state"], "REQUEST_CHANGES")

        retry_rt = self.fresh_runtime("status-failed-retryable")
        retry_rt["executor"].fail_next = True
        retry = retry_rt["engine"].process(self.make_md(retry_rt, hid="status-failed-retryable"))
        self.assertEqual(retry["new_state"]["delivery_state"], "FAILED_RETRYABLE")
        self.assertIsNotNone(retry["new_state"]["pending_delivery"])
        outcomes["FAILED_RETRYABLE"] = self.runtime_observability(retry_rt, retry)
        outcomes["FAILED_RETRYABLE"]["result"] = retry["result"]

        def disable_tl(manifest):
            manifest["role_destination_registry"]["TL"]["enabled"] = False
        final_rt = self.fresh_runtime("status-failed-final", disable_tl)
        final = final_rt["engine"].process(self.make_md(final_rt, hid="status-failed-final"))
        self.assertEqual(final["new_state"]["delivery_state"], "FAILED_FINAL")
        self.assertIsNone(final["new_state"]["pending_delivery"])
        outcomes["FAILED_FINAL"] = self.runtime_observability(final_rt, final)
        outcomes["FAILED_FINAL"]["result"] = final["result"]

        self.assertEqual(list((Path(self.tmp.name) / "status-needs_decision" / "artifacts/sample_workspace/outboxes/TL").glob("NEXT_*.json")), [])
        write_gate_evidence("NWIN-009", {"status": "PASS", "classifications": outcomes})

    def test_NWIN_010_routing_and_cross_project_isolation(self):
        cases = {}
        correct = self.fresh_runtime("route-correct")
        correct_result = correct["engine"].process(self.make_md(correct, hid="route-correct"))
        self.assertEqual(correct_result["validation_result"], "accepted")
        packet_path = next((correct["root"] / "artifacts/sample_workspace/outboxes/TL").glob("NEXT_*.json"))
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        self.assertEqual(packet["to"], "TL")
        self.assertEqual(packet["project_id"], correct["manifest"]["project_id"])
        self.assertEqual(packet["workflow_id"], correct["manifest"]["workflow_id"])
        self.assertTrue(packet.get("route_decision_id"))
        self.assertTrue(packet.get("idempotency_key"))
        cases["correct_route"] = self.runtime_observability(correct, correct_result) | {"packet": packet}

        wrong = self.fresh_runtime("route-wrong-recipient")
        wrong_result = wrong["engine"].process(self.make_md(wrong, recipient="QA", hid="wrong-recipient"))
        self.assertEqual(wrong_result["validation_result"], "wrong-recipient")
        self.assertEqual(wrong["engine"].store.load()["current_owner_role"], "WORKER")
        cases["wrong_recipient"] = self.runtime_observability(wrong, wrong_result)

        unknown = self.fresh_runtime("route-unknown")
        unknown_result = unknown["engine"].process(self.make_md(unknown, recipient="GHOST", hid="unknown-recipient"))
        self.assertEqual(unknown_result["validation_result"], "unknown-recipient")
        cases["unknown_destination"] = self.runtime_observability(unknown, unknown_result)

        def disable_tl(manifest):
            manifest["role_destination_registry"]["TL"]["enabled"] = False
        disabled = self.fresh_runtime("route-disabled", disable_tl)
        disabled_result = disabled["engine"].process(self.make_md(disabled, hid="disabled-route"))
        self.assertEqual(disabled_result["new_state"]["delivery_state"], "FAILED_FINAL")
        self.assertEqual(list((disabled["root"] / "artifacts/sample_workspace/outboxes/TL").glob("NEXT_*.json")), [])
        cases["disabled_destination"] = self.runtime_observability(disabled, disabled_result)

        body = self.fresh_runtime("route-body-mismatch")
        body_result = body["engine"].process(self.make_md(body, hid="body-route", extra="\n- To: PM\n- Endpoint: C:\\outside\\attacker\n"))
        body_packet_path = next((body["root"] / "artifacts/sample_workspace/outboxes/TL").glob("NEXT_*.json"))
        body_packet = json.loads(body_packet_path.read_text(encoding="utf-8"))
        self.assertEqual(body_packet["to"], "TL")
        self.assertEqual(body["executor"].last_path_decision["observed_path"], body["manifest"]["role_destination_registry"]["TL"]["endpoint_ref"])
        cases["body_header_mismatch"] = self.runtime_observability(body, body_result)

        spoof = self.fresh_runtime("route-spoof")
        spoof_result = spoof["engine"].process(self.make_md(spoof, sender="TL", recipient="QA", hid="spoof-role"))
        self.assertEqual(spoof_result["validation_result"], "unexpected-sender")
        cases["spoofed_role"] = self.runtime_observability(spoof, spoof_result)

        other = self.fresh_runtime("route-other-work")
        other_path = self.make_md(other, hid="other-project", work="OTHER-WORK-ITEM")
        other_result = other["engine"].process(other_path)
        self.assertEqual(other_result["validation_result"], "wrong-work-item")
        self.assertEqual(other["engine"].store.load()["accepted_handoff_ids"], [])
        cases["other_project_work_item"] = self.runtime_observability(other, other_result)

        # Separate active manifests remain root-scoped even with a valid-looking
        # work-item string. The packet receives only the active manifest's IDs.
        def other_manifest(manifest):
            manifest["project_id"] = "Orbit-Isolation-Fixture"
            manifest["workflow_id"] = "isolated-workflow"
        isolated = self.fresh_runtime("route-isolated-manifest", other_manifest)
        isolated_result = isolated["engine"].process(self.make_md(isolated, hid="isolated-valid-looking"))
        isolated_packet_path = next((isolated["root"] / "artifacts/sample_workspace/outboxes/TL").glob("NEXT_*.json"))
        isolated_packet = json.loads(isolated_packet_path.read_text(encoding="utf-8"))
        self.assertEqual(isolated_packet["project_id"], "Orbit-Isolation-Fixture")
        self.assertEqual(isolated_packet["workflow_id"], "isolated-workflow")
        self.assertNotEqual(isolated_packet["workflow_id"], correct["manifest"]["workflow_id"])
        cases["active_manifest_isolation"] = self.runtime_observability(isolated, isolated_result) | {"packet": isolated_packet}

        write_gate_evidence("NWIN-010", {"status": "PASS", "routing_cases": cases})

    def test_NWIN_011_trace_and_secret_canary_scan(self):
        canaries = dict(TRACE_CANARIES)
        body_rt = self.fresh_runtime("trace-body")
        extra = (
            f"\nDiagnostic text: {canaries['handoff_body']}\n"
            f"Malformed path: ..\\{canaries['malformed_path_text']}\\outside\n"
            f"Credential=Bearer-{canaries['simulated_credential']}\n"
        )
        body_result = body_rt["engine"].process(self.make_md(body_rt, hid="trace-body", extra=extra))
        self.assertEqual(body_result["validation_result"], "accepted")

        zip_rt = self.fresh_runtime("trace-zip")
        zip_path = zip_rt["inbox"] / "HANDOFF_M0-WF-WIN-001_WORKER_TO_TL.zip"
        handoff = HANDOFF_TMPL.format(work="M0-WF-WIN-001", sender="WORKER", recipient="TL", status="COMPLETE", handoff_id="trace-zip", sequence=1, extra="")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("HANDOFF.md", handoff)
            zf.writestr(f"artifacts/{canaries['archive_member']}.txt", "safe fixture payload")
        zip_result = zip_rt["engine"].process(zip_path)
        self.assertEqual(zip_result["validation_result"], "accepted")

        reason_rt = self.fresh_runtime("trace-reason")
        reason_result = reason_rt["engine"].process(self.make_md(reason_rt, recipient="QA", hid="trace-reason"))
        self.assertEqual(reason_result["validation_result"], "wrong-recipient")
        receipts = read_receipts(reason_rt["root"] / "artifacts/sample_workspace/receipts/receipts.jsonl")
        self.assertEqual(receipts[-1]["reason_code"], "wrong-recipient")
        self.assertTrue(receipts[-1]["receipt_id"])

        sinks = []
        for runtime in [body_rt, zip_rt, reason_rt]:
            ws = runtime["root"] / "artifacts/sample_workspace"
            sinks.extend([ws / "state.json", ws / "receipts/receipts.jsonl"])
            sinks.extend(ws.glob("outboxes/*/NEXT_*.json"))
            sinks.extend(ws.glob("escalation/NEXT_*.json"))
            sinks.extend(ws.glob("decisions/NEXT_*.json"))
        scan = scan_canaries(sinks, canaries)
        self.assertEqual(scan["status"], "PASS")
        self.assertTrue(all(not hits for hits in scan["hits_by_label"].values()))
        write_gate_evidence("NWIN-011", {
            "status": "PASS",
            "trace_canary_scan_result": scan,
            "diagnostic_reason_code": receipts[-1]["reason_code"],
            "diagnostic_receipt_id": receipts[-1]["receipt_id"],
            "raw_sensitive_input_in_delivery_packet": False,
            "bounded_trace_sinks": [str(path) for path in sinks],
        })


    def test_LIVE003_NATIVE_001_bootstrap_junction_escape_has_zero_outside_writes(self):
        root = Path(self.tmp.name) / "live003-bootstrap-junction"
        shutil.copytree(self.source_root / "artifacts", root / "artifacts")
        config_path = root / "artifacts/live003_bootstrap_config.json"
        manifest = json.loads(config_path.read_text(encoding="utf-8"))
        approved = root / "artifacts/live_trial"
        approved.mkdir(parents=True, exist_ok=True)
        outside = Path(self.tmp.name) / "live003-bootstrap-outside"
        outside.mkdir(parents=True, exist_ok=True)
        workspace = approved / "M0-WF-LIVE-003"
        self.assertIsNotNone(_winapi, "LIVE003 native bootstrap fixture requires CPython junction helper")
        try:
            _winapi.CreateJunction(str(outside), str(workspace))
            self.assertTrue(workspace.is_junction())
            with self.assertRaises(BootstrapError) as raised:
                bootstrap_workspace(
                    root,
                    manifest,
                    project_id="Orbit",
                    workflow_id="orbit-m0-live-trial",
                    work_item="M0-WF-LIVE-003",
                )
            self.assertEqual(list(outside.rglob("*")), [])
            write_gate_evidence("LIVE003-NWIN-001", {
                "status": "PASS",
                "fixture": "directory-junction",
                "exception_type": type(raised.exception).__name__,
                "reason": str(raised.exception),
                "outside_write_count": 0,
                "accepted_state_created": False,
            })
        finally:
            if workspace.exists() or workspace.is_junction():
                workspace.rmdir()


    def test_LIVE003_NATIVE_002_bootstrap_powershell_launcher_relative_paths_and_idempotency(self):
        root = Path(self.tmp.name) / "live003-bootstrap-launcher"
        shutil.copytree(self.source_root / "artifacts", root / "artifacts")

        workspace = root / "artifacts/live_trial/M0-WF-LIVE-003"
        if workspace.exists():
            shutil.rmtree(workspace)

        system_root = Path(os.environ.get("SystemRoot", r"C:\\Windows"))
        powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        self.assertTrue(powershell.is_file(), f"Windows PowerShell 5.1 executable not found: {powershell}")

        version = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertTrue(version.startswith("5.1"), f"Expected Windows PowerShell 5.1, got {version!r}")

        launcher = root / "artifacts/windows/run_bootstrap.ps1"
        launcher_text = launcher.read_text(encoding="utf-8")
        lowered_launcher = launcher_text.lower()
        self.assertNotIn("runas", lowered_launcher)
        self.assertNotIn("start-process", lowered_launcher)
        self.assertNotIn("-itemtype symboliclink", lowered_launcher)

        command = [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            r".\artifacts\windows\run_bootstrap.ps1",
            "-Root",
            ".",
            "-Config",
            r".\artifacts\live003_bootstrap_config.json",
            "-ProjectId",
            "Orbit",
            "-WorkflowId",
            "orbit-m0-live-trial",
            "-WorkItem",
            "M0-WF-LIVE-003",
        ]

        first = subprocess.run(command, cwd=root, text=True, capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_lines = [line for line in first.stdout.splitlines() if line.strip()]
        self.assertTrue(first_lines, "Bootstrap launcher produced no JSON output")
        first_result = json.loads(first_lines[-1])
        self.assertEqual(first_result["status"], "INITIALIZED")
        self.assertEqual(first_result["project_id"], "Orbit")
        self.assertEqual(first_result["workflow_id"], "orbit-m0-live-trial")
        self.assertEqual(first_result["work_item"], "M0-WF-LIVE-003")
        self.assertEqual(Path(first_result["workspace"]).resolve(), workspace.resolve())
        self.assertNotIn("sample_workspace", str(first_result["workspace"]))
        self.assertEqual(first_result["state_revision"], 1)
        self.assertEqual(first_result["executor_catalog"], ["PLACE_PACKET"])

        state_path = workspace / "state.json"
        manifest_path = workspace / "manifest.json"
        receipts_path = workspace / "receipts/receipts.jsonl"
        self.assertTrue(state_path.is_file())
        self.assertTrue(manifest_path.is_file())
        self.assertTrue(receipts_path.is_file())

        state = json.loads(state_path.read_text(encoding="utf-8"))
        generated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            (state["project_id"], state["workflow_id"], state["work_item"]),
            ("Orbit", "orbit-m0-live-trial", "M0-WF-LIVE-003"),
        )
        self.assertEqual(
            (generated_manifest["project_id"], generated_manifest["workflow_id"], generated_manifest["work_item"]),
            ("Orbit", "orbit-m0-live-trial", "M0-WF-LIVE-003"),
        )
        self.assertEqual(state["state_revision"], 1)
        self.assertEqual(generated_manifest["allowed_executor_operations"], ["PLACE_PACKET"])

        before_files = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file())
        before_hashes = {
            "state": file_digest(state_path),
            "manifest": file_digest(manifest_path),
            "receipts": file_digest(receipts_path),
        }

        second = subprocess.run(command, cwd=root, text=True, capture_output=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        second_lines = [line for line in second.stdout.splitlines() if line.strip()]
        self.assertTrue(second_lines, "Repeat bootstrap launcher produced no JSON output")
        second_result = json.loads(second_lines[-1])
        self.assertEqual(second_result["status"], "ALREADY_INITIALIZED")
        self.assertEqual(second_result["state_revision"], 1)
        self.assertEqual(second_result["executor_catalog"], ["PLACE_PACKET"])

        after_files = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file())
        after_hashes = {
            "state": file_digest(state_path),
            "manifest": file_digest(manifest_path),
            "receipts": file_digest(receipts_path),
        }
        self.assertEqual(after_files, before_files)
        self.assertEqual(after_hashes, before_hashes)

        write_gate_evidence("LIVE003-NWIN-002", {
            "status": "PASS",
            "powershell_version": version,
            "launcher": str(launcher),
            "caller_cwd": str(root),
            "root_argument": ".",
            "config_argument": r".\artifacts\live003_bootstrap_config.json",
            "first_status": first_result["status"],
            "second_status": second_result["status"],
            "resolved_identity": {
                "project_id": state["project_id"],
                "workflow_id": state["workflow_id"],
                "work_item": state["work_item"],
            },
            "workspace": str(workspace),
            "non_fixture_workspace": "sample_workspace" not in str(workspace),
            "state_revision_first": first_result["state_revision"],
            "state_revision_second": second_result["state_revision"],
            "executor_catalog": first_result["executor_catalog"],
            "authority_file_inventory_unchanged": before_files == after_files,
            "authority_hashes_unchanged": before_hashes == after_hashes,
            "receipts_sha256": after_hashes["receipts"],
            "elevation_requested": False,
            "developer_mode_dependency": False,
        })


    def test_LIVE003_NATIVE_003_launcher_resolves_python_without_py_launcher(self):
        """The operator launchers must not depend on the optional 'py' launcher.

        The Windows Python launcher is absent on Microsoft Store installs and on
        python.org installs where it was deselected. Before this gate existed the
        launchers invoked 'py -3' directly, so native evidence silently depended
        on whatever interpreter shim happened to be on PATH during validation.
        """
        root = Path(self.tmp.name) / "live003-python-resolution"
        shutil.copytree(self.source_root / "artifacts", root / "artifacts")

        workspace = root / "artifacts/live_trial/M0-WF-LIVE-003"
        if workspace.exists():
            shutil.rmtree(workspace)

        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        system32 = system_root / "System32"
        self.assertTrue(powershell.is_file(), f"Windows PowerShell 5.1 not found: {powershell}")

        command = [
            str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", r".\artifacts\windows\run_bootstrap.ps1",
            "-Root", ".",
            "-Config", r".\artifacts\live003_bootstrap_config.json",
            "-ProjectId", "Orbit",
            "-WorkflowId", "orbit-m0-live-trial",
            "-WorkItem", "M0-WF-LIVE-003",
        ]

        # Case 1: a 'py' that resolves but cannot run anything must fall back to
        # python.exe rather than aborting the launcher.
        shim_dir = root / "broken-py-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        (shim_dir / "py.cmd").write_text("@echo off\r\nexit /b 1\r\n", encoding="ascii")
        fallback_env = os.environ.copy()
        fallback_env["PATH"] = str(shim_dir) + os.pathsep + fallback_env.get("PATH", "")

        fallback = subprocess.run(command, cwd=root, text=True, capture_output=True, env=fallback_env)
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        fallback_lines = [line for line in fallback.stdout.splitlines() if line.strip()]
        self.assertTrue(fallback_lines, "Bootstrap launcher produced no JSON output under py fallback")
        fallback_result = json.loads(fallback_lines[-1])
        self.assertEqual(fallback_result["status"], "INITIALIZED")
        self.assertEqual(fallback_result["executor_catalog"], ["PLACE_PACKET"])

        state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (state["project_id"], state["workflow_id"], state["work_item"]),
            ("Orbit", "orbit-m0-live-trial", "M0-WF-LIVE-003"),
        )
        self.assertEqual(state["state_revision"], 1)

        # Case 2: with no Python 3 reachable at all the launcher must fail closed
        # with a stable reason code and must not create accepted workflow state.
        denied_root = Path(self.tmp.name) / "live003-python-absent"
        shutil.copytree(self.source_root / "artifacts", denied_root / "artifacts")
        denied_workspace = denied_root / "artifacts/live_trial/M0-WF-LIVE-003"
        if denied_workspace.exists():
            shutil.rmtree(denied_workspace)

        bare_env = os.environ.copy()
        bare_env["PATH"] = str(system32)
        bare_env.pop("PYTHONHOME", None)

        denied = subprocess.run(command, cwd=denied_root, text=True, capture_output=True, env=bare_env)
        combined = (denied.stdout or "") + (denied.stderr or "")
        self.assertNotEqual(denied.returncode, 0, "Launcher must fail closed with no interpreter")
        self.assertIn("orbit-python-interpreter-not-found", combined)
        self.assertFalse(
            (denied_workspace / "state.json").exists(),
            "Failed interpreter resolution must not create accepted workflow state",
        )

        launcher_sources = sorted(
            (root / "artifacts/windows").glob("run_*.ps1")
        )
        residual_py = {
            path.name: "py -3" not in path.read_text(encoding="utf-8")
            for path in launcher_sources
        }
        self.assertTrue(
            all(residual_py.values()),
            f"launcher still hard-codes the optional py launcher: {residual_py}",
        )

        write_gate_evidence("LIVE003-NWIN-003", {
            "status": "PASS",
            "fallback_status": fallback_result["status"],
            "fallback_state_revision": fallback_result["state_revision"],
            "fallback_executor_catalog": fallback_result["executor_catalog"],
            "broken_py_shim_on_path": True,
            "absent_interpreter_returncode": denied.returncode,
            "absent_interpreter_reason_code": "orbit-python-interpreter-not-found",
            "accepted_state_created_on_failure": False,
            "launchers_free_of_hard_py_dependency": residual_py,
            "elevation_requested": False,
            "developer_mode_dependency": False,
        })


if __name__ == "__main__":
    unittest.main(verbosity=2)
