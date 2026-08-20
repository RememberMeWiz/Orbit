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

    def test_surface_ready(self):
        res = self.adapter.surface_ready()
        self.assertTrue(res["ok"])
        self.assertEqual(res["reason_code"], "workspace-valid")

    def test_focus(self):
        self.assertTrue(self.adapter.focus("repository-steward")["ok"])
        self.assertFalse(self.adapter.focus("unknown-role")["ok"])

    def test_deliver_and_collect_receipt(self):
        # Deliver packet
        del_res = self.adapter.deliver(
            ledger=self.ledger,
            request_id="pm-req-001",
            endpoint_id="repository-steward",
            message="Test packet payload",
            verify_token="test-token",
            expected_sha256="abc1234",
        )
        self.assertTrue(del_res.ok)
        self.assertEqual(del_res.delivery_state, "DELIVERED")

        # Fake steward creating a receipt
        receipt_name = "STEWARD_RECEIPT_WORK-TEST.md"
        receipt_file = self.receipts_dir / receipt_name
        receipt_file.write_text("# Steward Receipt Content", encoding="utf-8")

        # Collect receipt
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
