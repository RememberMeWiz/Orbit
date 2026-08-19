"""Standalone runtime coverage.

The north-star question these tests answer: if every external AI service
disappears, how much of Orbit still works? OFFLINE-002 in particular does not
merely assert that no vendor module is imported -- it severs socket creation
entirely and then runs a full multi-role orchestration to completion.
"""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path

from workflow.core.bootstrap import bootstrap_workspace
from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.runtime import resolve_runtime_paths
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler

from standalone.agents import AgentTask, AgentTaskStore, LocalAgentRuntime
from standalone.agents.runtime import AgentRuntimeError
from standalone.brain import (
    BrainRouter,
    DeterministicBrain,
    LocalBrainRequest,
    LocalBrainResult,
    LocalModelBrain,
    validate_result,
)
from standalone.scheduler import LocalScheduler, SchedulerLedger

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class NoNetwork:
    """Context manager that makes any socket creation fail loudly."""

    def __init__(self):
        self._real = socket.socket
        self._real_conn = socket.create_connection

    def __enter__(self):
        def deny(*args, **kwargs):
            raise AssertionError("standalone runtime attempted a network call")

        socket.socket = deny
        socket.create_connection = deny
        return self

    def __exit__(self, *exc):
        socket.socket = self._real
        socket.create_connection = self._real_conn
        return False


class StandaloneBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orbit"
        shutil.copytree(PACKAGE_ROOT, self.root / "artifacts")

        config = json.loads((self.root / "artifacts/live003_bootstrap_config.json").read_text(encoding="utf-8"))
        workspace = self.root / "artifacts/live_trial/M0-WF-LIVE-003"
        if workspace.exists():
            shutil.rmtree(workspace)
        self.boot = bootstrap_workspace(
            self.root, config,
            project_id="Orbit", workflow_id="orbit-m0-live-trial", work_item="M0-WF-LIVE-003",
        )
        self.manifest = load_manifest(self.root, Path(self.boot["manifest_path"]))
        self.paths = resolve_runtime_paths(self.root, self.manifest)
        self.work_item = self.manifest["work_item"]

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, brain=None, handlers=None):
        router = BrainRouter([LocalModelBrain(), DeterministicBrain(handlers=handlers)] if brain is None else [brain])
        store = AgentTaskStore(self.paths.workspace / "agent_tasks.json", work_item=self.work_item)
        runtime = LocalAgentRuntime(store, router, stop_path=self.paths.stop)
        executor = PlacePacketExecutor(self.root, self.manifest)
        engine = WorkflowEngine(self.root, self.manifest, executor)
        reconciler = WorkspaceReconciler(self.root, self.manifest, engine)
        ledger = SchedulerLedger(self.paths.workspace / "scheduler_state.json", work_item=self.work_item)
        scheduler = LocalScheduler(
            root=self.root, manifest=self.manifest, engine=engine,
            reconciler=reconciler, runtime=runtime, ledger=ledger,
        )
        return {"router": router, "store": store, "runtime": runtime, "engine": engine, "scheduler": scheduler}


class OfflineTests(StandaloneBase):
    def test_OFFLINE_001_boots_with_no_vendor_credentials(self):
        import os

        vendor = [k for k in os.environ if k.startswith(("ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_API"))]
        saved = {k: os.environ.pop(k) for k in vendor}
        try:
            built = self.build()
            state = built["engine"].store.load()
            self.assertEqual(state["current_owner_role"], "WORKER")
            self.assertEqual(self.manifest["allowed_executor_operations"], ["PLACE_PACKET"])
        finally:
            os.environ.update(saved)

    def test_OFFLINE_002_full_orchestration_makes_no_network_calls(self):
        built = self.build()
        with NoNetwork():
            transcript = built["scheduler"].run(max_ticks=6)
        actions = [t["action"] for t in transcript]
        self.assertIn("ADVANCED", actions)
        self.assertEqual(built["engine"].store.load()["current_owner_role"], "QA")

    def test_OFFLINE_003_core_starts_with_no_external_adapters(self):
        # Only the deterministic local provider is registered: no cloud adapter
        # exists at all, and the core must still be fully operational.
        router = BrainRouter([DeterministicBrain()])
        self.assertEqual([p.name for p in router.eligible()], ["deterministic-local"])
        result = router.reason(LocalBrainRequest(task_id="t", role="WORKER", objective="o"))
        self.assertEqual(result.status, "OK")

    def test_OFFLINE_004_network_provider_ineligible_by_default(self):
        class CloudBrain:
            name = "cloud"
            requires_network = True

            def available(self):
                return True

            def reason(self, request):
                raise AssertionError("cloud provider must not be selected by default")

        router = BrainRouter([CloudBrain(), DeterministicBrain()])
        result = router.reason(LocalBrainRequest(task_id="t", role="WORKER", objective="o"))
        self.assertEqual(result.provider, "deterministic-local")

    def test_OFFLINE_005_quota_exhausted_provider_falls_back(self):
        class ExhaustedBrain:
            name = "exhausted"
            requires_network = False

            def available(self):
                return True

            def reason(self, request):
                raise RuntimeError("quota exhausted")

        router = BrainRouter([ExhaustedBrain(), DeterministicBrain()])
        result = router.reason(LocalBrainRequest(task_id="t", role="WORKER", objective="o"))
        self.assertEqual(result.status, "OK")
        self.assertEqual(result.provider, "deterministic-local")

    def test_OFFLINE_006_no_provider_yields_typed_blocked_not_crash(self):
        class DownBrain:
            name = "down"
            requires_network = False

            def available(self):
                return False

            def reason(self, request):
                raise AssertionError("unreachable")

        router = BrainRouter([DownBrain()])
        result = router.reason(LocalBrainRequest(task_id="t", role="WORKER", objective="o"))
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.reason_code, "brain-no-provider-available")


class BrainTests(StandaloneBase):
    def test_BRAIN_001_result_schema_enforced(self):
        request = LocalBrainRequest(
            task_id="t", role="WORKER", objective="o",
            result_schema={"required": ["summary", "artifacts"]},
        )
        bad = LocalBrainResult(task_id="t", status="OK", result={"summary": "only one"})
        checked = validate_result(request, bad)
        self.assertEqual(checked.status, "FAILED_FINAL")
        self.assertEqual(checked.reason_code, "brain-result-schema-violation")

    def test_BRAIN_002_brain_package_cannot_touch_workflow_state(self):
        # Structural, not incidental: the brain package must not import any
        # durable-state type, so a model result has no path to mutate state.
        brain_dir = PACKAGE_ROOT / "standalone/brain"
        for path in brain_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("StateStore", "WorkflowEngine", "atomic_write_json", "workflow.core.state", "workflow.core.engine"):
                self.assertNotIn(forbidden, text, f"{path.name} must not reach durable state via {forbidden}")

    def test_BRAIN_003_cannot_self_grant_capabilities(self):
        request = LocalBrainRequest(task_id="t", role="WORKER", objective="o", allowed_capabilities=("READ_FILE",))
        greedy = LocalBrainResult(
            task_id="t", status="OK", result={"summary": "s"},
            used_capabilities=("READ_FILE", "RUN_APPROVED_PROCESS"),
        )
        checked = validate_result(request, greedy)
        self.assertEqual(checked.status, "FAILED_FINAL")
        self.assertEqual(checked.reason_code, "brain-capability-escalation")
        self.assertIn("RUN_APPROVED_PROCESS", checked.detail)

    def test_AUTH_001_result_cannot_carry_authority_directives(self):
        request = LocalBrainRequest(task_id="t", role="WORKER", objective="o")
        hostile = LocalBrainResult(
            task_id="t", status="OK",
            result={"summary": "s", "allowed_executor_operations": ["RUN_COMMAND"], "destinations": {"X": "../../etc"}},
        )
        checked = validate_result(request, hostile)
        self.assertEqual(checked.status, "FAILED_FINAL")
        self.assertEqual(checked.reason_code, "brain-authority-directive-rejected")

    def test_AUTH_002_external_adapter_cannot_become_core_authority(self):
        # An external provider may answer, but its answer passes through exactly
        # the same validation gate; it gets no privileged path.
        class CloudBrain:
            name = "cloud"
            requires_network = True

            def available(self):
                return True

            def reason(self, request):
                return LocalBrainResult(
                    task_id=request.task_id, status="OK",
                    result={"summary": "s"}, used_capabilities=("RUN_COMMAND",), provider="cloud",
                )

        router = BrainRouter([CloudBrain()], allow_network=True)
        result = router.reason(LocalBrainRequest(task_id="t", role="WORKER", objective="o"))
        self.assertEqual(result.status, "FAILED_FINAL")
        self.assertEqual(result.reason_code, "brain-capability-escalation")

    def test_BRAIN_004_task_mismatch_rejected(self):
        request = LocalBrainRequest(task_id="expected", role="WORKER", objective="o")
        other = LocalBrainResult(task_id="other", status="OK", result={})
        checked = validate_result(request, other)
        self.assertEqual(checked.reason_code, "brain-result-task-mismatch")


class AgentTests(StandaloneBase):
    def make_task(self, role="WORKER", objective="objective-1"):
        return AgentTask(work_item=self.work_item, role=role, objective=objective)

    def test_AGENT_001_worker_task_returns_structured_result(self):
        built = self.build()
        task = built["runtime"].run(self.make_task())
        self.assertEqual(task.status, "READY_FOR_REVIEW")
        self.assertEqual(task.result["provider"], "deterministic-local")
        self.assertIn("summary", task.result["result"])
        self.assertEqual(task.attempts, 1)

    def test_AGENT_002_blocked_is_preserved(self):
        def blocked(request):
            return LocalBrainResult(task_id=request.task_id, status="BLOCKED", reason_code="needs-input", detail="missing spec")

        built = self.build(handlers={"WORKER": blocked})
        task = built["runtime"].run(self.make_task())
        self.assertEqual(task.status, "BLOCKED")
        self.assertEqual(task.result["reason_code"], "needs-input")

    def test_AGENT_003_needs_decision_is_preserved(self):
        def decide(request):
            return LocalBrainResult(task_id=request.task_id, status="NEEDS_DECISION", reason_code="scope-question")

        built = self.build(handlers={"WORKER": decide})
        task = built["runtime"].run(self.make_task())
        self.assertEqual(task.status, "NEEDS_DECISION")

    def test_AGENT_004_restart_does_not_duplicate_task(self):
        built = self.build()
        first = built["runtime"].run(self.make_task())
        self.assertEqual(first.attempts, 1)

        # Fresh runtime over the same durable store == process restart.
        restarted = self.build()
        again = restarted["runtime"].run(self.make_task())
        self.assertEqual(again.task_id, first.task_id)
        self.assertEqual(again.attempts, 1, "restart must not re-run a resolved task")
        self.assertEqual(len(restarted["store"].all_tasks()), 1)

    def test_AGENT_005_stop_prevents_new_agent_work(self):
        built = self.build()
        self.paths.stop.write_text("stopped", encoding="utf-8")
        task = built["runtime"].run(self.make_task())
        self.assertEqual(task.status, "ASSIGNED")
        self.assertEqual(task.attempts, 0)

    def test_AGENT_006_wrong_work_item_task_rejected(self):
        built = self.build()
        alien = AgentTask(work_item="M0-WF-SOMETHING-ELSE", role="WORKER", objective="o")
        with self.assertRaises(AgentRuntimeError):
            built["runtime"].run(alien)

    def test_AGENT_007_complete_unreachable_from_brain_output(self):
        built = self.build()
        task = built["runtime"].run(self.make_task())
        self.assertNotEqual(task.status, "COMPLETE")
        # Only governed code may complete, and only from READY_FOR_REVIEW.
        blocked_task = AgentTask(work_item=self.work_item, role="TL", objective="o2", status="BLOCKED")
        with self.assertRaises(AgentRuntimeError):
            built["runtime"].mark_complete(blocked_task, evidence={})


class SchedulerTests(StandaloneBase):
    def test_SCHED_001_worker_to_tl_advances_exactly_once(self):
        built = self.build()
        outcome = built["scheduler"].tick()
        self.assertEqual(outcome["action"], "ADVANCED")
        self.assertEqual((outcome["owner"], outcome["recipient"]), ("WORKER", "TL"))
        state = built["engine"].store.load()
        self.assertEqual(state["current_owner_role"], "TL")
        packets = list((self.paths.workspace / "outboxes/TL").glob("NEXT_*.json"))
        self.assertEqual(len(packets), 1)

    def test_SCHED_002_full_local_chain_reaches_approval_gate(self):
        built = self.build()
        transcript = built["scheduler"].run(max_ticks=6)
        actions = [t["action"] for t in transcript]
        # WORKER -> TL -> QA locally, then the QA->PM approval gate surfaces.
        self.assertEqual(actions[:2], ["ADVANCED", "ADVANCED"])
        self.assertIn("AWAITING_APPROVAL", actions)
        self.assertEqual(built["engine"].store.load()["current_owner_role"], "QA")

    def test_SCHED_003_blocked_role_routes_locally_and_surfaces(self):
        def blocked(request):
            return LocalBrainResult(task_id=request.task_id, status="BLOCKED", reason_code="qa-failure", detail="defect found")

        built = self.build(handlers={"WORKER": blocked})
        outcome = built["scheduler"].tick()
        self.assertEqual(outcome["handoff_status"], "BLOCKED")
        state = built["engine"].store.load()
        self.assertEqual(state["work_state"], "BLOCKED")
        # Routed to the registered escalation destination, not guessed.
        self.assertEqual(len(list((self.paths.workspace / "escalation").glob("NEXT_*.json"))), 1)
        self.assertEqual(built["scheduler"].tick()["action"], "AWAITING_HUMAN")

    def test_SCHED_004_product_decision_stops_instead_of_guessing(self):
        def decide(request):
            return LocalBrainResult(task_id=request.task_id, status="NEEDS_DECISION", reason_code="scope-ambiguous")

        built = self.build(handlers={"WORKER": decide})
        built["scheduler"].tick()
        state = built["engine"].store.load()
        self.assertEqual(state["work_state"], "NEEDS_DECISION")
        self.assertEqual(len(list((self.paths.workspace / "decisions").glob("NEXT_*.json"))), 1)
        self.assertEqual(built["scheduler"].tick()["action"], "AWAITING_HUMAN")

    def test_SCHED_005_stop_halts_scheduling(self):
        built = self.build()
        self.paths.stop.write_text("stopped", encoding="utf-8")
        self.assertEqual(built["scheduler"].tick()["action"], "STOPPED")
        self.assertEqual(built["engine"].store.load()["current_owner_role"], "WORKER")

    def test_SCHED_006_restart_mid_chain_does_not_double_advance(self):
        built = self.build()
        built["scheduler"].tick()
        state_after_first = built["engine"].store.load()
        self.assertEqual(state_after_first["current_owner_role"], "TL")

        restarted = self.build()
        outcome = restarted["scheduler"].tick()
        self.assertEqual(outcome["owner"], "TL", "restart must resume at TL, not redo WORKER")
        self.assertEqual(len(list((self.paths.workspace / "outboxes/TL").glob("NEXT_*.json"))), 1)

    def test_SCHED_007_no_human_transport_in_the_local_chain(self):
        """Every internal handoff is produced by Orbit, not carried by a person."""
        built = self.build()
        built["scheduler"].run(max_ticks=6)
        receipts = (self.paths.receipts).read_text(encoding="utf-8").splitlines()
        accepted = [json.loads(r) for r in receipts if r.strip()]
        advanced = [r for r in accepted if r.get("validation_result") == "accepted"]
        self.assertGreaterEqual(len(advanced), 2)
        for record in advanced:
            self.assertTrue(str(record["handoff_id"]).startswith("local-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
