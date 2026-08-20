"""Common transport adapter contract for Orbit.

Unifies outbound and inbound communication across different tool surfaces
without broadening execution authority.

Status Classifications:
- LIVE: Connected and validated against real desktop/remote surface.
- CONTRACT_ONLY: Formal typed contract defined; no live external actuation claimed.
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
    """Typed transport adapter definition for the Antigravity Repository Steward.

    Status: CONTRACT_ONLY.
    Defines the typed interface for transporting accepted PM packets and
    collecting signed steward receipts without claiming live external Antigravity
    actuation or exposing arbitrary Git/shell authority.
    """

    TRANSPORT_STATUS = "CONTRACT_ONLY"

    def __init__(self, workspace_root: Path, receipts_dir: Optional[Path] = None):
        self.workspace_root = Path(workspace_root)
        self.receipts_dir = Path(receipts_dir) if receipts_dir else self.workspace_root / "receipts"
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    def surface_ready(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "status": self.TRANSPORT_STATUS,
            "reason_code": "steward-contract-only",
            "data": {
                "workspace_root": str(self.workspace_root),
                "transport_status": self.TRANSPORT_STATUS,
                "live_transport_connected": False,
            },
        }

    def focus(self, endpoint_id: str) -> Dict[str, Any]:
        if endpoint_id in ("repository-steward", "git-steward"):
            return {"ok": True, "status": self.TRANSPORT_STATUS, "reason_code": "steward-focused"}
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

        # Record intent in ledger as STAGED_VERIFIED, not DELIVERED
        ledger.begin(
            request_id=request_id,
            endpoint_id=endpoint_id,
            message_digest=expected_sha256 or "packet-digest",
        )
        record = ledger.mark_staged(request_id, artifact_digest="", message_digest=expected_sha256 or "packet-digest")

        # Refuse to claim fabricated external delivery success
        return ChatTransportResult(
            ok=True,
            operation="SEND_BOUNDED_MESSAGE",
            reason_code="steward-staged-contract-only",
            detail="Packet staged and verified; external Antigravity transport is CONTRACT_ONLY.",
            delivery_state="STAGED_CONTRACT_ONLY",
            data=record,
        )

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
