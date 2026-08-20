"""PM-supervised apprenticeship loop.

Driver stubbed, so these assert the loop's decisions rather than the app's
behaviour. The properties under test are the two that make the loop safe:
PM decides routing, and nothing external happens twice.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from standalone.bridge import DeliveryLedger, PMBridgeState, TeachingTraceStore
from standalone.bridge.orchestrator import ApprenticeLoop
from standalone.bridge.pm_envelope import PMDirective, PMRequest, request_identity
from standalone.tests.test_chatgpt_adapter import StubDriver, build

WORK_ITEM = "M0-WF-LOOP-TEST"


def envelope(request_id, *, directive_id="dir-1", work_item=WORK_ITEM,
             action="DISPATCH_TO_ROLE", target="windows-workflow"):
    body = "\n".join([
        "version: 0.1",
        f"request_id: {request_id}",
        f"directive_id: {directive_id}",
        f"work_item: {work_item}",
        f"action: {action}",
        f"target_endpoint: {target}",
    ])
    return "ChatGPT said:\nLooks good.\n\n```\nORBIT_DIRECTIVE\n" + body + "\n```\n"


class LoopBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.driver = StubDriver()
        self.adapter = build(self.driver)
        self.stop = self.dir / "STOP"

    def tearDown(self):
        self.tmp.cleanup()

    def loop(self, **kw) -> ApprenticeLoop:
        return ApprenticeLoop(
            adapter=self.adapter,
            pm_state=PMBridgeState(self.dir / "pm.json", work_item=WORK_ITEM),
            ledger=DeliveryLedger(self.dir / "delivery.json", work_item=WORK_ITEM),
            traces=TeachingTraceStore(self.dir / "traces.jsonl", work_item=WORK_ITEM),
            work_item=WORK_ITEM,
            inbox_dir=self.dir / "inbox",
            stop_path=self.stop,
            sleeper=lambda _s: None,
            clock=_Clock(),
            **kw,
        )


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 5.0
        return self.t


class WakeTests(LoopBase):
    def test_LOOP_001_wake_posts_and_records_pending(self):
        loop = self.loop()
        out = loop.wake_pm(reason="bridge-online", nonce="n1")
        self.assertEqual(out.action, "PM_WOKEN")
        pending = loop.pm_state.load()["pending_request"]
        self.assertEqual(pending["request_id"], out.data["request_id"])
        self.assertIn("press_send", self.driver.calls)

    def test_LOOP_002_failed_post_does_not_open_a_pending_request(self):
        """Orbit must not wait for an answer to a question PM never received."""
        from standalone.tests.test_chatgpt_adapter import deny as stub_deny
        self.driver.send_result = stub_deny("send-control-disabled")
        loop = self.loop()
        out = loop.wake_pm(reason="bridge-online", nonce="n1")
        self.assertEqual(out.action, "WAKE_FAILED")
        self.assertIsNone(loop.pm_state.load()["pending_request"])

    def test_LOOP_004_the_request_shows_pm_exactly_how_to_answer(self):
        """PM must not need out-of-band knowledge of the envelope schema."""
        loop = self.loop()
        out = loop.wake_pm(reason="bridge-online", nonce="n1")
        posted = self.driver.composer_text

        self.assertIn("ORBIT_PM_REQUEST", posted)
        self.assertIn("ORBIT_DIRECTIVE", posted)
        for field in ("version:", "request_id:", "directive_id:", "work_item:",
                      "action:", "target_endpoint:"):
            self.assertIn(field, posted, field)
        # The id PM has to quote back appears verbatim, not as a placeholder.
        self.assertIn(out.data["request_id"], posted)

    def test_LOOP_005_the_request_names_only_endpoints_orbit_would_accept(self):
        loop = self.loop()
        loop.wake_pm(reason="bridge-online", nonce="n1")
        posted = self.driver.composer_text
        for endpoint_id in loop.adapter.registry.enabled_ids():
            self.assertIn(endpoint_id, posted)

    def test_LOOP_006_offering_a_target_does_not_authorise_it(self):
        """The list is advisory; resolution is still the only authority."""
        loop = self.loop()
        registry = loop.adapter.registry
        self.assertNotIn("some-chat-from-prose", registry.enabled_ids())
        out = loop.dispatch(
            directive=PMDirective(directive_id="d", request_id="r", work_item=WORK_ITEM,
                                  action="DISPATCH_TO_ROLE",
                                  target_endpoint="some-chat-from-prose"),
            assignment="x TOKEN", verify_token="TOKEN")
        self.assertEqual(out.action, "DISPATCH_FAILED")
        self.assertIn("endpoint-not-registered", out.reason_code)

    def test_LOOP_003_stop_prevents_waking_pm(self):
        self.stop.write_text("stopped", encoding="utf-8")
        out = self.loop().wake_pm(reason="bridge-online", nonce="n1")
        self.assertEqual(out.action, "STOPPED")
        self.assertNotIn("press_send", self.driver.calls)


class DirectiveTests(LoopBase):
    def open_request(self, loop):
        request = PMRequest(request_id=request_identity(WORK_ITEM, "r", "n1"),
                            work_item=WORK_ITEM, reason="r")
        loop.pm_state.open_request(request)
        return request

    def test_LOOP_010_valid_directive_is_accepted(self):
        loop = self.loop()
        request = self.open_request(loop)
        self.driver.transcript = envelope(request.request_id)
        out = loop.await_directive(timeout=30.0)
        self.assertEqual(out.action, "DIRECTIVE_ACCEPTED")
        self.assertEqual(out.data["directive"]["target_endpoint"], "windows-workflow")

    def test_LOOP_011_stale_request_id_is_reported_not_waited_out(self):
        loop = self.loop()
        self.open_request(loop)
        self.driver.transcript = envelope("pmreq-someoldthing")
        out = loop.await_directive(timeout=1000.0)
        self.assertEqual(out.action, "DIRECTIVE_REJECTED")
        self.assertEqual(out.reason_code, "directive-stale-request-id")

    def test_LOOP_012_prose_only_keeps_waiting(self):
        loop = self.loop()
        self.open_request(loop)
        self.driver.transcript = "ChatGPT said:\nyes go ahead, dispatch it please"
        out = loop.await_directive(timeout=30.0)
        self.assertEqual(out.action, "AWAITING_PM")
        self.assertEqual(out.reason_code, "directive-absent")

    def test_LOOP_013_stop_halts_waiting(self):
        loop = self.loop()
        self.open_request(loop)
        self.stop.write_text("stopped", encoding="utf-8")
        self.assertEqual(loop.await_directive(timeout=30.0).action, "STOPPED")


class DispatchTests(LoopBase):
    def directive(self, **kw):
        return PMDirective(directive_id=kw.get("directive_id", "dir-1"),
                           request_id="pmreq-1", work_item=WORK_ITEM,
                           action=kw.get("action", "DISPATCH_TO_ROLE"),
                           target_endpoint=kw.get("target", "windows-workflow"))

    def test_LOOP_020_dispatch_sends_to_the_named_endpoint(self):
        loop = self.loop()
        out = loop.dispatch(directive=self.directive(), assignment="TOKEN-1 body", verify_token="TOKEN-1")
        self.assertEqual(out.action, "DISPATCHED")
        self.assertEqual(out.data["endpoint_id"], "windows-workflow")
        self.assertEqual(out.data["delivery_state"], "SENT_UNCONFIRMED")

    def test_LOOP_021_repeat_dispatch_of_same_directive_is_inert(self):
        """Re-running a directive must not send twice."""
        loop = self.loop()
        d = self.directive()
        first = loop.dispatch(directive=d, assignment="TOKEN-1 body", verify_token="TOKEN-1")
        self.assertEqual(first.action, "DISPATCHED")
        sends = self.driver.calls.count("press_send")

        second = loop.dispatch(directive=d, assignment="TOKEN-1 body", verify_token="TOKEN-1")
        self.assertEqual(second.action, "DISPATCH_FAILED")
        self.assertEqual(second.reason_code, "awaiting-confirmation")
        self.assertEqual(self.driver.calls.count("press_send"), sends)

    def test_LOOP_022_directive_without_target_is_refused(self):
        out = self.loop().dispatch(directive=self.directive(target=""),
                                   assignment="x", verify_token="")
        self.assertEqual(out.action, "DIRECTIVE_INCOMPLETE")

    def test_LOOP_023_non_dispatch_action_is_not_executed_as_dispatch(self):
        out = self.loop().dispatch(directive=self.directive(action="HOLD"),
                                   assignment="x", verify_token="")
        self.assertEqual(out.action, "UNSUPPORTED_ACTION")
        self.assertNotIn("press_send", self.driver.calls)

    def test_LOOP_024_stop_prevents_dispatch(self):
        self.stop.write_text("stopped", encoding="utf-8")
        out = self.loop().dispatch(directive=self.directive(), assignment="x", verify_token="")
        self.assertEqual(out.action, "STOPPED")
        self.assertNotIn("press_send", self.driver.calls)

    def test_LOOP_025_unregistered_target_fails_closed(self):
        out = self.loop().dispatch(directive=self.directive(target="some-chat-from-prose"),
                                   assignment="x TOKEN", verify_token="TOKEN")
        self.assertEqual(out.action, "DISPATCH_FAILED")
        self.assertIn("endpoint-not-registered", out.reason_code)


class TraceTests(LoopBase):
    def test_LOOP_030_supervised_decision_is_recorded(self):
        loop = self.loop()
        d = PMDirective(directive_id="dir-9", request_id="pmreq-9", work_item=WORK_ITEM,
                        action="DISPATCH_TO_ROLE", target_endpoint="windows-workflow")
        loop.record(directive=d, action="DISPATCH_TO_ROLE",
                    state_before={"work_state": "READY"}, state_after={"delivery": "SENT_UNCONFIRMED"},
                    evidence={"endpoint": "windows-workflow"}, result="sent",
                    classification="success", reason="worker-dispatch")
        records = loop.traces.all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["directive_id"], "dir-9")

    def test_LOOP_031_traces_never_self_promote(self):
        loop = self.loop()
        self.assertFalse(hasattr(loop.traces, "promote"))
        self.assertEqual(loop.traces.propose_promotion(threshold=1), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
