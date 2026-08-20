"""The full zero-courier cycle, end to end.

A scripted driver stands in for the app so the *sequence* can be asserted
offline: PM is asked, PM answers, the worker is dispatched to, the worker is
waited for, the result is collected and validated, and PM is told. The live
equivalent is recorded in the run journal instead.

The headline assertion is a negative one, repeated in several forms: at no
point does a human carry anything, and no failure anywhere in the chain is
papered over by improvising past it.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from standalone.bridge import DeliveryLedger, PMBridgeState, TeachingTraceStore
from standalone.bridge.accessibility import READY, GuardOutcome
from standalone.bridge.orchestrator import ApprenticeLoop
from standalone.bridge.roundtrip import STEPS, RoundTrip
from standalone.tests.test_chatgpt_adapter import StubDriver, build, ok

WORK_ITEM = "M0-WF-ROUNDTRIP-TEST"
ARTIFACT = "HANDOFF_M0-WF-ROUNDTRIP-TEST_WORKER_TO_ORBIT.md"
TOKEN = "ORBIT-ASSIGNMENT-TOKEN"

# What a real worker returns: a formal handoff the collector will actually
# accept. Written out in full rather than stubbed, because the validation on
# the way in is part of what is being tested.
WORKER_HANDOFF = """# Worker Result

## Header
- Work Item: `M0-WF-ROUNDTRIP-TEST`
- From: `WORKER`
- To: `ORBIT`
- Status: `COMPLETE`
- Handoff ID: `M0-WF-ROUNDTRIP-TEST-0001`
- Sequence: `1`

## Summary
Assignment carried out; nothing required a courier.
"""


class ReadyGuard:
    """Stands in for the accessibility guard; records if it was consulted."""

    def __init__(self, outcome: GuardOutcome = None):
        self.outcome = outcome or GuardOutcome(READY, "ok", state={"accessibility_ready": True})
        self.calls: List[bool] = []

    def ensure(self, *, allow_launch: bool = True) -> GuardOutcome:
        self.calls.append(allow_launch)
        return self.outcome


class ScriptedDriver(StubDriver):
    """A PM that answers with a directive and a worker that produces a file."""

    def __init__(self, *, directive_text: str = None, artifact_name: str = ARTIFACT,
                 worker_hangs: bool = False, **kw):
        super().__init__(**kw)
        self.surface = {**self.surface,
                        "chat_items": ["Orbit PM", "Windows Workflow"]}
        self.directive_text = directive_text
        self.artifact_name = artifact_name
        self.worker_hangs = worker_hangs
        self.saved: List[Dict[str, str]] = []
        self.sent_messages: List[str] = []
        # Streaming is per-conversation and decays with time, exactly as in the
        # real app: sending to PM must not make the worker chat look busy, and
        # PM must not still look busy after the whole worker cycle has run.
        self._busy_until: Dict[str, int] = {}
        self._hanging: Dict[str, bool] = {}

    # A conversation is idle until something is sent to it, then it streams
    # briefly and settles -- which is what the adapter waits on. Modelling it
    # this way rather than as a fixed list means send's own "is it already
    # streaming?" check sees the same world the waiter does.
    def response_state(self):
        self.calls.append("response_state")
        here = self.header
        streaming = self._hanging.get(here, False) or self._tick < self._busy_until.get(here, 0)
        return ok({"state": "streaming" if streaming else "idle",
                   "send_present": not streaming, "stop_present": streaming})

    def press_send(self):
        result = super().press_send()
        if result.ok:
            if self.worker_hangs and self.header == "Windows Workflow":
                self._hanging[self.header] = True
            else:
                self._busy_until[self.header] = self._tick + 2
        return result

    # PM's transcript answers whatever request is currently open.
    def read_transcript_tail(self, max_chars=6000):
        self.calls.append("read_transcript_tail")
        if self.directive_text is not None:
            return ok({"text": self.directive_text, "nodes": 1,
                       "total_length": len(self.directive_text)})
        body = "\n".join([
            "version: 0.1",
            f"request_id: {self._open_request_id()}",
            "directive_id: dir-roundtrip-1",
            f"work_item: {WORK_ITEM}",
            "action: DISPATCH_TO_ROLE",
            "target_endpoint: windows-workflow",
        ])
        text = "ChatGPT said:\nGo ahead.\n\n```\nORBIT_DIRECTIVE\n" + body + "\n```\n"
        return ok({"text": text, "nodes": 1, "total_length": len(text)})

    def _open_request_id(self) -> str:
        # Mirrors what Orbit just posted, the way a real PM reply would.
        for message in reversed(self.sent_messages):
            for line in message.splitlines():
                if line.strip().startswith("request_id:"):
                    return line.split(":", 1)[1].strip()
        return "unknown"

    def set_message(self, text):
        result = super().set_message(text)
        self.sent_messages.append(text)
        return result

    def list_artifacts(self):
        self.calls.append("list_artifacts")
        return ok({"saveable": [self.artifact_name], "previewable": []})

    def save_artifact_as(self, *, filename, destination):
        self.calls.append("save_artifact_as")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text(WORKER_HANDOFF, encoding="utf-8")
        self.saved.append({"filename": filename, "destination": destination})
        return ok({"filename": filename, "destination": destination})

    def call(self, operation, params=None):
        params = params or {}
        if operation == "save_artifact_as":
            return self.save_artifact_as(**params)
        if operation == "read_transcript_tail":
            return self.read_transcript_tail(**params)
        return getattr(self, operation)()

    @property
    def _tick(self) -> int:
        """Driver calls made so far. One unit of elapsed time, which is what
        lets a conversation stop looking busy on its own."""
        return len(self.calls)


class CycleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.driver = ScriptedDriver()
        self.guard = ReadyGuard()
        self.journal = self.dir / "journal.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def make_loop(self) -> ApprenticeLoop:
        return ApprenticeLoop(
            adapter=build(self.driver),
            pm_state=PMBridgeState(self.dir / "pm.json", work_item=WORK_ITEM),
            ledger=DeliveryLedger(self.dir / "delivery.json", work_item=WORK_ITEM),
            traces=TeachingTraceStore(self.dir / "traces.jsonl", work_item=WORK_ITEM),
            work_item=WORK_ITEM,
            inbox_dir=self.dir / "inbox",
            stop_path=self.dir / "STOP",
            sleeper=lambda _s: None,
            clock=_Clock(),
        )

    def cycle(self, **kw) -> RoundTrip:
        return RoundTrip(self.make_loop(), journal_path=self.journal,
                         guard=self.guard, **kw)

    def run_cycle(self, **overrides):
        params = dict(reason="worker assignment ready", nonce="rt-1",
                      assignment=f"{TOKEN}\nPlease produce {ARTIFACT}.",
                      verify_token=TOKEN, expected_artifact=ARTIFACT,
                      pm_timeout=60.0, worker_timeout=60.0)
        params.update(overrides)
        return self.cycle().run(**params)


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 5.0
        return self.t


class HappyPathTests(CycleBase):
    def setUp(self):
        super().setUp()
        self.result = self.run_cycle()

    def test_RT_001_the_cycle_completes(self):
        self.assertTrue(self.result.completed, self.result.reason_code)
        self.assertEqual(self.result.reason_code, "ok")
        self.assertEqual(self.result.stopped_at, "")

    def test_RT_002_no_human_carried_anything(self):
        self.assertEqual(self.result.courier_actions, 0)

    def test_RT_003_every_step_ran_in_order(self):
        self.assertEqual([s["step"] for s in self.result.steps], list(STEPS))

    def test_RT_004_pm_chose_the_target_not_orbit(self):
        self.assertEqual(self.result.directive["target_endpoint"], "windows-workflow")
        self.assertEqual(self.result.directive["action"], "DISPATCH_TO_ROLE")

    def test_RT_005_the_assignment_reached_the_worker_chat(self):
        self.assertIn("focus_chat:Windows Workflow", self.driver.calls)
        self.assertTrue(any(TOKEN in m for m in self.driver.sent_messages))

    def test_RT_006_the_artifact_was_materialised_and_hashed(self):
        self.assertEqual(self.result.artifact["filename"], ARTIFACT)
        path = Path(self.result.artifact["path"])
        self.assertTrue(path.exists())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.result.artifact["sha256"], digest)

    def test_RT_007_pm_was_told_with_the_digest(self):
        final = self.result.steps[-1]
        self.assertEqual(final["step"], "report_to_pm")
        self.assertEqual(final["action"], "PM_WOKEN")
        report = self.driver.sent_messages[-1]
        self.assertIn(self.result.artifact["sha256"][:16], report)

    def test_RT_008_two_messages_were_posted_one_to_each_conversation(self):
        """Wake PM, dispatch worker, report to PM."""
        self.assertEqual(self.driver.calls.count("press_send"), 3)

    def test_RT_009_the_decision_was_recorded_as_a_teaching_trace(self):
        traces = self.make_loop().traces.all()
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["directive_id"], "dir-roundtrip-1")
        self.assertEqual(traces[0]["classification"], "success")

    def test_RT_010_the_directive_is_consumed_so_it_cannot_replay(self):
        consumed = self.make_loop().pm_state.load()["consumed_directive_ids"]
        self.assertIn("dir-roundtrip-1", consumed)


class JournalTests(CycleBase):
    def test_RT_020_every_step_is_journalled_durably(self):
        self.run_cycle()
        lines = [json.loads(l) for l in self.journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l["step"] for l in lines], list(STEPS))
        for line in lines:
            self.assertEqual(line["work_item"], WORK_ITEM)
            self.assertTrue(line["at"])

    def test_RT_021_an_interrupted_cycle_is_readable_up_to_where_it_stopped(self):
        self.driver.directive_text = "ChatGPT said:\nsure, go ahead"      # prose, never a directive
        result = self.run_cycle()
        self.assertFalse(result.completed)
        self.assertEqual(result.stopped_at, "await_directive")
        lines = [json.loads(l) for l in self.journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l["step"] for l in lines], ["preflight", "wake_pm", "await_directive"])

    def test_RT_022_observer_sees_each_step_as_it_happens(self):
        seen = []
        self.cycle(observer=lambda step, entry: seen.append(step)).run(
            reason="r", nonce="n", assignment=f"{TOKEN} x", verify_token=TOKEN,
            expected_artifact=ARTIFACT, pm_timeout=60.0, worker_timeout=60.0)
        self.assertEqual(seen, list(STEPS))


class HaltTests(CycleBase):
    """Each failure must stop the cycle where it stands."""

    def test_RT_030_blocked_surface_stops_before_anything_is_posted(self):
        from standalone.bridge.accessibility import NEEDS_HUMAN_RESTART

        self.guard.outcome = GuardOutcome(NEEDS_HUMAN_RESTART, "accessibility-flag-absent",
                                          remedy="restart it")
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "preflight")
        self.assertEqual(result.reason_code, "accessibility-flag-absent")
        self.assertNotIn("press_send", self.driver.calls)

    def test_RT_031_a_directive_for_a_different_work_item_is_refused(self):
        self.driver.directive_text = (
            "ChatGPT said:\n```\nORBIT_DIRECTIVE\nversion: 0.1\nrequest_id: whatever\n"
            "directive_id: d\nwork_item: SOME-OTHER-ITEM\naction: DISPATCH_TO_ROLE\n"
            "target_endpoint: windows-workflow\n```")
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "await_directive")
        self.assertEqual(self.driver.calls.count("press_send"), 1)   # only the wake

    def test_RT_032_a_target_pm_never_registered_is_refused(self):
        self.driver.directive_text = (
            "ChatGPT said:\n```\nORBIT_DIRECTIVE\nversion: 0.1\nrequest_id: PLACEHOLDER\n"
            "directive_id: d\nwork_item: " + WORK_ITEM + "\naction: DISPATCH_TO_ROLE\n"
            "target_endpoint: some-chat-from-prose\n```")
        result = self.cycle().run(
            reason="r", nonce="n", assignment=f"{TOKEN} x", verify_token=TOKEN,
            expected_artifact=ARTIFACT, pm_timeout=60.0, worker_timeout=60.0)
        # Stops at directive matching or dispatch, but never sends to the target.
        self.assertFalse(result.completed)
        self.assertNotIn("focus_chat:some-chat-from-prose", self.driver.calls)

    def test_RT_033_a_missing_artifact_is_not_reported_as_success(self):
        self.driver.artifact_name = "SOMETHING_ELSE.md"
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "collect")
        self.assertIsNone(result.artifact)
        self.assertFalse(result.completed)
        # PM is told, but told the truth: a blocker, never a delivered artifact.
        report = self.driver.sent_messages[-1]
        self.assertIn("BLOCKED at collect", report)
        self.assertNotIn("artifact_sha256", report)

    def test_RT_034_a_worker_that_never_finishes_stops_before_collecting(self):
        self.driver.worker_hangs = True
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "await_worker")
        self.assertNotIn("save_artifact_as", self.driver.calls)

    def test_RT_035_stop_file_halts_the_cycle_at_the_first_step(self):
        (self.dir / "STOP").write_text("stopped", encoding="utf-8")
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "wake_pm")
        self.assertNotIn("press_send", self.driver.calls)

    def test_RT_036_a_failed_wake_never_opens_a_pending_request(self):
        from standalone.tests.test_chatgpt_adapter import deny as stub_deny

        self.driver.send_result = stub_deny("send-control-disabled")
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "wake_pm")
        self.assertIsNone(self.make_loop().pm_state.load()["pending_request"])


class BlockerReportTests(CycleBase):
    """Once work is in flight, a stall must not be silent."""

    def test_RT_050_a_stalled_worker_is_reported_to_pm(self):
        self.driver.worker_hangs = True
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "await_worker")
        self.assertEqual(result.steps[-1]["step"], "report_blocker")
        self.assertIn("BLOCKED at await_worker", self.driver.sent_messages[-1])

    def test_RT_051_a_failed_collection_is_reported_to_pm(self):
        self.driver.artifact_name = "SOMETHING_ELSE.md"
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "collect")
        self.assertEqual(result.steps[-1]["step"], "report_blocker")

    def test_RT_052_nothing_is_announced_before_work_is_dispatched(self):
        """A cycle that never asked its question should not start reporting."""
        self.driver.directive_text = "sure, go ahead"
        result = self.run_cycle()
        self.assertEqual(result.stopped_at, "await_directive")
        self.assertNotIn("report_blocker", [s["step"] for s in result.steps])

    def test_RT_053_a_blocked_surface_reports_nothing_at_all(self):
        from standalone.bridge.accessibility import NEEDS_HUMAN_RESTART

        self.guard.outcome = GuardOutcome(NEEDS_HUMAN_RESTART, "accessibility-flag-absent")
        result = self.run_cycle()
        self.assertNotIn("report_blocker", [s["step"] for s in result.steps])
        self.assertNotIn("press_send", self.driver.calls)

    def test_RT_054_a_report_that_itself_fails_is_journalled_not_raised(self):
        """The blocker is usually the surface, so the report often fails too."""
        self.driver.worker_hangs = True

        loop = self.make_loop()
        original = loop.report_to_pm
        loop.report_to_pm = lambda **kw: (_ for _ in ()).throw(RuntimeError("surface gone"))
        cycle = RoundTrip(loop, journal_path=self.journal, guard=self.guard)
        result = cycle.run(reason="r", nonce="n", assignment=f"{TOKEN} x",
                           verify_token=TOKEN, expected_artifact=ARTIFACT,
                           pm_timeout=60.0, worker_timeout=60.0)
        self.assertEqual(result.stopped_at, "await_worker")
        last = result.steps[-1]
        self.assertEqual(last["step"], "report_blocker")
        self.assertEqual(last["action"], "BLOCKER_REPORT_FAILED")
        self.assertEqual(original.__name__, "report_to_pm")


class GuardIntegrationTests(CycleBase):
    def test_RT_040_the_surface_is_checked_before_every_cycle(self):
        self.run_cycle()
        self.assertEqual(len(self.guard.calls), 1)

    def test_RT_041_launch_can_be_withheld_for_supervised_runs(self):
        RoundTrip(self.make_loop(), journal_path=self.journal, guard=self.guard,
                  allow_launch=False).run(
            reason="r", nonce="n", assignment=f"{TOKEN} x", verify_token=TOKEN,
            expected_artifact=ARTIFACT, pm_timeout=60.0, worker_timeout=60.0)
        self.assertEqual(self.guard.calls, [False])


if __name__ == "__main__":
    unittest.main(verbosity=2)
