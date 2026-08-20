"""Restart and recovery across the whole apprenticeship loop.

Every test here restarts by constructing a *fresh* loop over the same state
files, which is exactly what a restarted Orbit process sees. Nothing is carried
in memory between the two halves of a test.

The invariant: no restart, at any point in the cycle, produces a duplicate
external effect or loses a decision that was already made.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from standalone.bridge import DeliveryLedger, PMBridgeState, TeachingTraceStore
from standalone.bridge.orchestrator import ApprenticeLoop
from standalone.bridge.pm_envelope import PMDirective, PMRequest, request_identity
from standalone.tests.test_chatgpt_adapter import StubDriver, build

WORK_ITEM = "M0-WF-RESTART-TEST"


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 5.0
        return self.t


def envelope(request_id, *, directive_id="dir-1", target="windows-workflow", action="DISPATCH_TO_ROLE"):
    body = "\n".join([
        "version: 0.1",
        f"request_id: {request_id}",
        f"directive_id: {directive_id}",
        f"work_item: {WORK_ITEM}",
        f"action: {action}",
        f"target_endpoint: {target}",
    ])
    return "```\nORBIT_DIRECTIVE\n" + body + "\n```"


class RestartBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.stop = self.dir / "STOP"
        self.driver = StubDriver()

    def tearDown(self):
        self.tmp.cleanup()

    def restart(self) -> ApprenticeLoop:
        """A fresh loop over the same files == a restarted process."""
        return ApprenticeLoop(
            adapter=build(self.driver),
            pm_state=PMBridgeState(self.dir / "pm.json", work_item=WORK_ITEM),
            ledger=DeliveryLedger(self.dir / "delivery.json", work_item=WORK_ITEM),
            traces=TeachingTraceStore(self.dir / "traces.jsonl", work_item=WORK_ITEM),
            work_item=WORK_ITEM,
            inbox_dir=self.dir / "inbox",
            stop_path=self.stop,
            sleeper=lambda _s: None,
            clock=_Clock(),
        )

    def directive(self, directive_id="dir-1", target="windows-workflow"):
        return PMDirective(directive_id=directive_id, request_id="pmreq-1",
                           work_item=WORK_ITEM, action="DISPATCH_TO_ROLE",
                           target_endpoint=target)


class WaitingRestartTests(RestartBase):
    def test_RST_001_restart_while_waiting_for_pm_keeps_the_pending_request(self):
        loop = self.restart()
        out = loop.wake_pm(reason="bridge-online", nonce="n1")
        request_id = out.data["request_id"]

        resumed = self.restart()
        pending = resumed.pm_state.load()["pending_request"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["request_id"], request_id)

    def test_RST_002_restart_then_directive_still_matches(self):
        loop = self.restart()
        request_id = loop.wake_pm(reason="bridge-online", nonce="n1").data["request_id"]

        resumed = self.restart()
        self.driver.transcript = envelope(request_id)
        out = resumed.await_directive(timeout=30.0)
        self.assertEqual(out.action, "DIRECTIVE_ACCEPTED")

    def test_RST_003_restart_does_not_repost_the_pm_request(self):
        """Waking again after a restart must not duplicate the question."""
        loop = self.restart()
        loop.wake_pm(reason="bridge-online", nonce="n1")
        sends = self.driver.calls.count("press_send")

        resumed = self.restart()
        again = resumed.wake_pm(reason="bridge-online", nonce="n1")
        self.assertEqual(again.action, "WAKE_FAILED")
        self.assertEqual(self.driver.calls.count("press_send"), sends)

    def test_RST_004_consumed_directive_stays_consumed_across_restart(self):
        loop = self.restart()
        request = PMRequest(request_id=request_identity(WORK_ITEM, "r", "n"),
                            work_item=WORK_ITEM, reason="r")
        loop.pm_state.open_request(request)
        self.driver.transcript = envelope(request.request_id)
        accepted = loop.await_directive(timeout=30.0)
        self.assertEqual(accepted.action, "DIRECTIVE_ACCEPTED")
        loop.consume(self.directive())

        resumed = self.restart()
        resumed.pm_state.open_request(request)
        replay = resumed.await_directive(timeout=30.0)
        self.assertEqual(replay.action, "DIRECTIVE_REJECTED")
        self.assertEqual(replay.reason_code, "directive-already-consumed")


class DispatchRestartTests(RestartBase):
    def test_RST_010_restart_after_dispatch_does_not_resend(self):
        loop = self.restart()
        out = loop.dispatch(directive=self.directive(), assignment="TOKEN body", verify_token="TOKEN")
        self.assertEqual(out.action, "DISPATCHED")
        sends = self.driver.calls.count("press_send")

        resumed = self.restart()
        again = resumed.dispatch(directive=self.directive(), assignment="TOKEN body", verify_token="TOKEN")
        self.assertEqual(again.action, "DISPATCH_FAILED")
        self.assertEqual(again.reason_code, "awaiting-confirmation")
        self.assertEqual(self.driver.calls.count("press_send"), sends)

    def test_RST_011_restart_staged_before_send_is_safely_retryable(self):
        """Nothing external happened, so a retry is correct and allowed."""
        led = DeliveryLedger(self.dir / "delivery.json", work_item=WORK_ITEM)
        led.begin(request_id="dispatch-dir-1", endpoint_id="windows-workflow",
                  artifact_digest="", message_digest="d")
        led.mark_staged("dispatch-dir-1", artifact_digest="", message_digest="d")

        resumed = self.restart()
        allowed, why = resumed.ledger.may_send("dispatch-dir-1")
        self.assertTrue(allowed, why)

    def test_RST_012_restart_after_actuation_is_ambiguous_and_never_resends(self):
        led = DeliveryLedger(self.dir / "delivery.json", work_item=WORK_ITEM)
        led.begin(request_id="dispatch-dir-1", endpoint_id="windows-workflow",
                  artifact_digest="", message_digest="d")
        led.mark_staged("dispatch-dir-1", artifact_digest="", message_digest="d")
        led.mark_actuating("dispatch-dir-1", artifact_digest="", message_digest="d")

        resumed = self.restart()
        out = resumed.dispatch(directive=self.directive(), assignment="x", verify_token="")
        self.assertEqual(out.action, "DISPATCH_FAILED")
        self.assertEqual(out.reason_code, "ambiguous-requires-human-disposition")
        self.assertNotIn("press_send", self.driver.calls)

    def test_RST_013_delivered_dispatch_is_not_repeated_after_restart(self):
        led = DeliveryLedger(self.dir / "delivery.json", work_item=WORK_ITEM)
        rid = "dispatch-dir-1"
        led.begin(request_id=rid, endpoint_id="windows-workflow", artifact_digest="", message_digest="d")
        led.mark_staged(rid, artifact_digest="", message_digest="d")
        led.mark_actuating(rid, artifact_digest="", message_digest="d")
        led.mark_sent(rid)
        led.mark_delivered(rid)

        resumed = self.restart()
        out = resumed.dispatch(directive=self.directive(), assignment="x", verify_token="")
        self.assertEqual(out.reason_code, "already-delivered")
        self.assertNotIn("press_send", self.driver.calls)

    def test_RST_014_a_different_directive_is_a_different_delivery(self):
        """Restart must not confuse two dispatches for one another."""
        loop = self.restart()
        loop.dispatch(directive=self.directive("dir-A"), assignment="A TOKEN", verify_token="TOKEN")

        resumed = self.restart()
        out = resumed.dispatch(directive=self.directive("dir-B"), assignment="B TOKEN", verify_token="TOKEN")
        self.assertEqual(out.action, "DISPATCHED")
        self.assertEqual(self.driver.calls.count("press_send"), 2)


class StopRestartTests(RestartBase):
    def test_RST_020_stop_survives_restart(self):
        self.stop.write_text("stopped", encoding="utf-8")
        resumed = self.restart()
        self.assertTrue(resumed.stopped())
        self.assertEqual(resumed.dispatch(directive=self.directive(),
                                          assignment="x", verify_token="").action, "STOPPED")

    def test_RST_021_clearing_stop_resumes_normally(self):
        self.stop.write_text("stopped", encoding="utf-8")
        self.assertEqual(self.restart().wake_pm(reason="r", nonce="n").action, "STOPPED")
        self.stop.unlink()
        self.assertEqual(self.restart().wake_pm(reason="r", nonce="n").action, "PM_WOKEN")

    def test_RST_022_stop_before_send_leaves_no_delivery_record(self):
        """A stopped work item must not leave state to reconcile later."""
        self.stop.write_text("stopped", encoding="utf-8")
        loop = self.restart()
        loop.dispatch(directive=self.directive(), assignment="x", verify_token="")
        self.assertEqual(self.restart().ledger.open_records(), {})


class TraceRestartTests(RestartBase):
    def test_RST_030_traces_survive_restart_and_append(self):
        loop = self.restart()
        d = self.directive("dir-T")
        loop.record(directive=d, action="DISPATCH_TO_ROLE", state_before={"work_state": "READY"},
                    state_after={}, evidence={}, result="sent", classification="success",
                    reason="dispatch")

        resumed = self.restart()
        resumed.record(directive=d, action="DISPATCH_TO_ROLE", state_before={"work_state": "READY"},
                       state_after={}, evidence={}, result="sent", classification="success",
                       reason="dispatch")
        self.assertEqual(len(resumed.traces.all()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
