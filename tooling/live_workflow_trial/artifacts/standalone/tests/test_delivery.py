"""Durable exactly-once delivery lifecycle.

These are the crash tests. A "restart" here is a fresh DeliveryLedger over the
same file, which is exactly what a restarted process sees.

The property that matters: no sequence of crashes can produce a second send of
something that may already have been sent.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from standalone.bridge.delivery import (
    DELIVERY_STATES,
    RETRYABLE_STATES,
    DeliveryError,
    DeliveryLedger,
    digest_text,
)

REQ = "treq-abc123"
ART = "a" * 64
MSG = digest_text("assignment body")


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "delivery.json"

    def tearDown(self):
        self.tmp.cleanup()

    def ledger(self, work_item="WI-1") -> DeliveryLedger:
        """A fresh ledger over the same file == a restarted process."""
        return DeliveryLedger(self.path, work_item=work_item)

    def staged(self) -> DeliveryLedger:
        led = self.ledger()
        led.begin(request_id=REQ, endpoint_id="windows-worker", artifact_digest=ART, message_digest=MSG)
        led.mark_staged(REQ, artifact_digest=ART, message_digest=MSG)
        return led


class LifecycleTests(LedgerBase):
    def test_DLV_001_happy_path_reaches_delivered(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        led.mark_sent(REQ)
        record = led.mark_delivered(REQ, evidence={"handoff_id": "h1"})
        self.assertEqual(record["state"], "DELIVERED")

    def test_DLV_002_states_are_the_declared_set(self):
        for state in ("PENDING_SEND", "STAGED_VERIFIED", "SEND_ACTUATED",
                      "SENT_UNCONFIRMED", "DELIVERED", "FAILED", "AMBIGUOUS"):
            self.assertIn(state, DELIVERY_STATES)

    def test_DLV_003_actuation_is_recorded_before_the_send(self):
        """The on-disk record must already say SEND_ACTUATED pre-press."""
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["records"][REQ]["state"], "SEND_ACTUATED")


class CrashTests(LedgerBase):
    def test_DLV_010_crash_before_actuation_is_retryable(self):
        led = self.staged()          # crash here: nothing external happened
        restarted = self.ledger()
        allowed, why = restarted.may_send(REQ)
        self.assertTrue(allowed, why)
        self.assertIn(restarted.get(REQ)["state"], RETRYABLE_STATES)

    def test_DLV_011_crash_after_actuation_becomes_ambiguous(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        # crash: the click may or may not have landed
        restarted = self.ledger()
        record = restarted.get(REQ)
        self.assertEqual(record["state"], "AMBIGUOUS")
        self.assertEqual(record["reason_code"], "crash-after-actuation")

    def test_DLV_012_ambiguous_never_auto_resends(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        restarted = self.ledger()
        allowed, why = restarted.may_send(REQ)
        self.assertFalse(allowed)
        self.assertEqual(why, "ambiguous-requires-human-disposition")

    def test_DLV_013_ambiguous_cannot_be_reopened_by_begin(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        restarted = self.ledger()
        record = restarted.begin(request_id=REQ, endpoint_id="windows-worker",
                                 artifact_digest=ART, message_digest=MSG)
        self.assertEqual(record["state"], "AMBIGUOUS")

    def test_DLV_014_reconciliation_is_persisted_not_just_returned(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        self.ledger().load()                      # first restart reconciles
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["records"][REQ]["state"], "AMBIGUOUS")

    def test_DLV_015_crash_after_send_returned_awaits_confirmation(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        led.mark_sent(REQ)
        restarted = self.ledger()
        allowed, why = restarted.may_send(REQ)
        self.assertFalse(allowed)
        self.assertEqual(why, "awaiting-confirmation")

    def test_DLV_016_delivered_is_never_resent(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        led.mark_sent(REQ)
        led.mark_delivered(REQ)
        allowed, why = self.ledger().may_send(REQ)
        self.assertFalse(allowed)
        self.assertEqual(why, "already-delivered")

    def test_DLV_017_failure_after_actuation_is_ambiguous_not_failed(self):
        """A driver error after the press does not prove nothing was sent."""
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        record = led.mark_failed(REQ, reason_code="driver-timeout")
        self.assertEqual(record["state"], "AMBIGUOUS")
        self.assertIn("driver-timeout", record["reason_code"])

    def test_DLV_018_failure_before_actuation_is_failed_and_retryable(self):
        led = self.staged()
        record = led.mark_failed(REQ, reason_code="composer-not-found")
        self.assertEqual(record["state"], "FAILED")
        allowed, _ = self.ledger().may_send(REQ)
        self.assertTrue(allowed)


class PayloadIntegrityTests(LedgerBase):
    def test_DLV_020_changed_artifact_between_staging_and_send_refused(self):
        led = self.staged()
        with self.assertRaises(DeliveryError) as ctx:
            led.mark_actuating(REQ, artifact_digest="b" * 64, message_digest=MSG)
        self.assertEqual(str(ctx.exception), "delivery-artifact-changed-since-staging")
        self.assertEqual(led.get(REQ)["state"], "STAGED_VERIFIED")

    def test_DLV_021_changed_message_between_staging_and_send_refused(self):
        led = self.staged()
        with self.assertRaises(DeliveryError) as ctx:
            led.mark_actuating(REQ, artifact_digest=ART, message_digest=digest_text("different"))
        self.assertEqual(str(ctx.exception), "delivery-message-changed-since-staging")

    def test_DLV_022_cannot_actuate_without_staging(self):
        led = self.ledger()
        led.begin(request_id=REQ, endpoint_id="e", artifact_digest=ART, message_digest=MSG)
        with self.assertRaises(DeliveryError):
            led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)

    def test_DLV_023_retry_increments_attempt(self):
        led = self.staged()
        led.mark_failed(REQ, reason_code="composer-not-found")
        record = self.ledger().begin(request_id=REQ, endpoint_id="windows-worker",
                                     artifact_digest=ART, message_digest=MSG)
        self.assertEqual(record["attempt"], 2)
        self.assertEqual(record["state"], "PENDING_SEND")


class IsolationTests(LedgerBase):
    def test_DLV_030_ledger_is_bound_to_one_work_item(self):
        self.staged()
        with self.assertRaises(DeliveryError) as ctx:
            self.ledger(work_item="OTHER-WORK-ITEM").load()
        self.assertEqual(str(ctx.exception), "delivery-ledger-work-item-mismatch")

    def test_DLV_031_distinct_requests_do_not_interfere(self):
        led = self.staged()
        led.begin(request_id="treq-other", endpoint_id="architecture-tl",
                  artifact_digest="c" * 64, message_digest=digest_text("other"))
        self.assertEqual(led.get(REQ)["state"], "STAGED_VERIFIED")
        self.assertEqual(led.get("treq-other")["state"], "PENDING_SEND")

    def test_DLV_032_malformed_ledger_raises_rather_than_resetting(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(DeliveryError) as ctx:
            self.ledger().load()
        self.assertEqual(str(ctx.exception), "delivery-ledger-malformed")

    def test_DLV_033_open_records_excludes_settled_ones(self):
        led = self.staged()
        led.mark_actuating(REQ, artifact_digest=ART, message_digest=MSG)
        led.mark_sent(REQ)
        led.mark_delivered(REQ)
        led.begin(request_id="treq-open", endpoint_id="e", artifact_digest=ART, message_digest=MSG)
        self.assertEqual(list(self.ledger().open_records()), ["treq-open"])


class DeliverIntegrationTests(LedgerBase):
    """deliver() must honour the ledger, not merely carry it along."""

    def build(self, **kw):
        import sys
        from standalone.tests.test_chatgpt_adapter import StubDriver, build
        driver = StubDriver(**kw)
        return driver, build(driver)

    def test_DLV_040_stop_prevents_send_entirely(self):
        driver, adapter = self.build()
        stop = Path(self.tmp.name) / "STOP"
        stop.write_text("stopped", encoding="utf-8")
        result = adapter.deliver(ledger=self.ledger(), request_id=REQ, endpoint_id="orbit-pm",
                                 message="body REQ", verify_token="REQ", stop_path=stop)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "stop-active")
        self.assertNotIn("press_send", driver.calls)
        # STOP must not even open a delivery record.
        self.assertIsNone(self.ledger().get(REQ))

    def test_DLV_041_happy_path_records_sent_unconfirmed(self):
        driver, adapter = self.build()
        result = adapter.deliver(ledger=self.ledger(), request_id=REQ, endpoint_id="orbit-pm",
                                 message="body REQ", verify_token="REQ")
        self.assertTrue(result.ok, result.reason_code)
        self.assertEqual(result.delivery_state, "SENT_UNCONFIRMED")
        self.assertEqual(self.ledger().get(REQ)["state"], "SENT_UNCONFIRMED")

    def test_DLV_042_ambiguous_request_is_refused_without_touching_the_app(self):
        led = self.ledger()
        led.begin(request_id=REQ, endpoint_id="orbit-pm", artifact_digest="", message_digest=digest_text("body REQ"))
        led.mark_staged(REQ, artifact_digest="", message_digest=digest_text("body REQ"))
        led.mark_actuating(REQ, artifact_digest="", message_digest=digest_text("body REQ"))
        self.ledger().load()   # restart -> AMBIGUOUS

        driver, adapter = self.build()
        result = adapter.deliver(ledger=self.ledger(), request_id=REQ, endpoint_id="orbit-pm",
                                 message="body REQ", verify_token="REQ")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "ambiguous-requires-human-disposition")
        self.assertEqual(driver.calls, [], "an ambiguous request must not touch the app at all")

    def test_DLV_043_delivered_request_is_not_resent(self):
        led = self.ledger()
        led.begin(request_id=REQ, endpoint_id="orbit-pm", artifact_digest="", message_digest=digest_text("b"))
        led.mark_staged(REQ, artifact_digest="", message_digest=digest_text("b"))
        led.mark_actuating(REQ, artifact_digest="", message_digest=digest_text("b"))
        led.mark_sent(REQ)
        led.mark_delivered(REQ)

        driver, adapter = self.build()
        result = adapter.deliver(ledger=self.ledger(), request_id=REQ, endpoint_id="orbit-pm",
                                 message="b", verify_token="")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "already-delivered")
        self.assertNotIn("press_send", driver.calls)

    def test_DLV_044_send_failure_after_actuation_is_ambiguous(self):
        from standalone.tests.test_chatgpt_adapter import deny as stub_deny
        driver, adapter = self.build(send_result=stub_deny("send-control-disabled"))
        result = adapter.deliver(ledger=self.ledger(), request_id=REQ, endpoint_id="orbit-pm",
                                 message="body REQ", verify_token="REQ")
        self.assertFalse(result.ok)
        self.assertEqual(self.ledger().get(REQ)["state"], "AMBIGUOUS")

    def test_DLV_045_staging_failure_before_actuation_stays_retryable(self):
        from standalone.tests.test_chatgpt_adapter import StubDriver, build, ok as stub_ok

        class Swallowing(StubDriver):
            def set_message(self, text):
                self.calls.append("set_message")
                self.composer_text = ""
                return stub_ok({"length": 0})

        driver = Swallowing()
        adapter = build(driver)
        result = adapter.deliver(ledger=self.ledger(), request_id=REQ, endpoint_id="orbit-pm",
                                 message="body REQ", verify_token="REQ")
        self.assertFalse(result.ok)
        self.assertEqual(self.ledger().get(REQ)["state"], "FAILED")
        allowed, _ = self.ledger().may_send(REQ)
        self.assertTrue(allowed)
        self.assertNotIn("press_send", driver.calls)

    def test_DLV_046_artifact_changed_before_send_is_refused(self):
        driver, adapter = self.build()
        art = Path(self.tmp.name) / "HANDOFF_WI-1_WORKER_TO_TL.md"
        art.write_bytes(b"original")

        real_stage = adapter.stage_message

        def mutate_then_stage(text, **kw):
            # Simulate the file changing between validation and the send gate.
            art.write_bytes(b"tampered")
            return real_stage(text, **kw)

        adapter.stage_message = mutate_then_stage
        result = adapter.deliver(ledger=self.ledger(), request_id=REQ, endpoint_id="orbit-pm",
                                 message="body REQ", verify_token="REQ", artifact_path=art)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-changed-before-send")
        self.assertNotIn("press_send", driver.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)