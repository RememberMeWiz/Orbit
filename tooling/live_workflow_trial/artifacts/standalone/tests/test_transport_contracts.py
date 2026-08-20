"""Tests for Orbit common transport contracts and Antigravity steward adapter."""
import tempfile
import unittest
from pathlib import Path

from standalone.bridge.contracts import ChatTransportResult
from standalone.bridge.delivery import DeliveryLedger
from standalone.bridge.transport_contracts import AntigravityStewardAdapter, BaseTransportAdapter


class TestTransportContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.receipts_dir = self.root / "receipts"
        self.inbox_dir = self.root / "inbox"
        self.inbox_dir.mkdir()
        self.adapter = AntigravityStewardAdapter(self.root, receipts_dir=self.receipts_dir)
        self.ledger = DeliveryLedger(self.root / "delivery.json", work_item="WORK-TEST")

    def tearDown(self):
        self.tmp.cleanup()

    def test_implements_interface(self):
        self.assertIsInstance(self.adapter, BaseTransportAdapter)

    def test_surface_ready_is_contract_only(self):
        res = self.adapter.surface_ready()
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "CONTRACT_ONLY")
        self.assertEqual(res["reason_code"], "steward-contract-only")
        self.assertFalse(res["data"]["live_transport_connected"])

    def test_focus(self):
        self.assertTrue(self.adapter.focus("repository-steward")["ok"])
        self.assertEqual(self.adapter.focus("repository-steward")["status"], "CONTRACT_ONLY")
        self.assertFalse(self.adapter.focus("unknown-role")["ok"])

    def test_deliver_stages_as_contract_only_without_claiming_fake_external_delivery(self):
        del_res = self.adapter.deliver(
            ledger=self.ledger,
            request_id="pm-req-001",
            endpoint_id="repository-steward",
            message="Test packet payload",
            verify_token="test-token",
            expected_sha256="abc1234",
        )
        self.assertTrue(del_res.ok)
        self.assertEqual(del_res.delivery_state, "STAGED_CONTRACT_ONLY")
        self.assertNotEqual(del_res.delivery_state, "DELIVERED")

        # Verify ledger state is STAGED_VERIFIED, never DELIVERED
        rec = self.ledger.get("pm-req-001")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["state"], "STAGED_VERIFIED")

    def test_collect_receipt_from_workspace(self):
        receipt_name = "STEWARD_RECEIPT_WORK-TEST.md"
        receipt_file = self.receipts_dir / receipt_name
        receipt_file.write_text("# Steward Receipt Content", encoding="utf-8")

        col_res = self.adapter.collect_artifact(
            endpoint_id="repository-steward",
            expected_name=receipt_name,
            inbox_dir=self.inbox_dir,
            work_item="WORK-TEST",
        )
        self.assertTrue(col_res["ok"])
        self.assertEqual(col_res["data"]["filename"], receipt_name)
        self.assertTrue(Path(col_res["data"]["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
