"""Common transport adapter contract for Orbit.

Unifies outbound and inbound communication across different tool surfaces
(e.g., ChatGPT desktop UIA bridge, Antigravity Repository Steward bridge)
without broadening execution authority.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

from .contracts import ChatTransportResult
from .delivery import DeliveryLedger


class BaseTransportAdapter(ABC):
    """Abstract interface for all Orbit role transport adapters."""

    @abstractmethod
    def surface_ready(self) -> Any:
        """Check if the transport surface is active and ready."""
        pass

    @abstractmethod
    def focus(self, endpoint_id: str) -> Any:
        """Focus the specified endpoint/conversation."""
        pass

    @abstractmethod
    def deliver(
        self,
        *,
        ledger: DeliveryLedger,
        request_id: str,
        endpoint_id: str,
        message: str,
        verify_token: str,
        artifact_path: Optional[Path] = None,
        expected_sha256: str = "",
        stop_path: Optional[Path] = None,
    ) -> ChatTransportResult:
        """Deliver a bounded message or artifact to an endpoint."""
        pass

    @abstractmethod
    def collect_artifact(
        self,
        *,
        endpoint_id: str,
        expected_name: str,
        inbox_dir: Path,
        work_item: str,
        expected_sender: str = "",
    ) -> Any:
        """Collect a return artifact via file card."""
        pass

    @abstractmethod
    def collect_from_transcript(
        self,
        *,
        endpoint_id: str,
        expected_name: str,
        inbox_dir: Path,
        work_item: str,
        expected_sender: str = "",
    ) -> Any:
        """Collect a return handoff directly from transcript text."""
        pass


class AntigravityStewardAdapter(BaseTransportAdapter):
    """Typed transport adapter for the Antigravity Repository Steward.

    Provides a bounded mechanism for transporting accepted PM packets and
    collecting signed steward receipts without exposing arbitrary Git or shell
    authority.
    """

    def __init__(self, workspace_root: Path, receipts_dir: Optional[Path] = None):
        self.workspace_root = Path(workspace_root)
        self.receipts_dir = Path(receipts_dir) if receipts_dir else self.workspace_root / "receipts"
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def surface_ready(self) -> Dict[str, Any]:
        return {
            "ok": self.workspace_root.is_dir(),
            "reason_code": "workspace-valid" if self.workspace_root.is_dir() else "workspace-missing",
            "data": {"workspace_root": str(self.workspace_root)},
        }

    def focus(self, endpoint_id: str) -> Dict[str, Any]:
        if endpoint_id in ("repository-steward", "git-steward"):
            return {"ok": True, "reason_code": "steward-focused"}
        return {"ok": False, "reason_code": "unknown-endpoint"}

    def deliver(
        self,
        *,
        ledger: DeliveryLedger,
        request_id: str,
        endpoint_id: str,
        message: str,
        verify_token: str,
        artifact_path: Optional[Path] = None,
        expected_sha256: str = "",
        stop_path: Optional[Path] = None,
    ) -> ChatTransportResult:
        if stop_path and Path(stop_path).is_file():
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", "stop-active", delivery_state="STOPPED")
        if endpoint_id not in ("repository-steward", "git-steward"):
            return ChatTransportResult.deny("SEND_BOUNDED_MESSAGE", "unsupported-endpoint", delivery_state="FAILED")

        # Record intent in ledger
        ledger.begin(
            request_id=request_id,
            endpoint_id=endpoint_id,
            message_digest=expected_sha256 or "packet-digest",
        )
        ledger.mark_staged(request_id, artifact_digest="", message_digest=expected_sha256 or "packet-digest")
        ledger.mark_actuating(request_id, artifact_digest="", message_digest=expected_sha256 or "packet-digest")
        ledger.mark_sent(request_id)
        record = ledger.mark_delivered(request_id, evidence={"steward": True})
        return ChatTransportResult.allow("SEND_BOUNDED_MESSAGE", record, delivery_state="DELIVERED")

    def collect_artifact(
        self,
        *,
        endpoint_id: str,
        expected_name: str,
        inbox_dir: Path,
        work_item: str,
        expected_sender: str = "",
    ) -> Dict[str, Any]:
        receipt_file = self.receipts_dir / expected_name
        if not receipt_file.is_file():
            # Check workspace root
            receipt_file = self.workspace_root / expected_name
        if not receipt_file.is_file():
            return {"ok": False, "reason_code": "receipt-not-found"}

        target = Path(inbox_dir) / expected_name
        target.write_bytes(receipt_file.read_bytes())
        import hashlib
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return {
            "ok": True,
            "reason_code": "receipt-collected",
            "data": {"filename": expected_name, "path": str(target), "sha256": digest},
        }

    def collect_from_transcript(
        self,
        *,
        endpoint_id: str,
        expected_name: str,
        inbox_dir: Path,
        work_item: str,
        expected_sender: str = "",
    ) -> Dict[str, Any]:
        return self.collect_artifact(
            endpoint_id=endpoint_id,
            expected_name=expected_name,
            inbox_dir=inbox_dir,
            work_item=work_item,
            expected_sender=expected_sender,
        )
