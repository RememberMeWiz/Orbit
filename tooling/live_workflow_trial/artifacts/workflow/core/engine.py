from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Tuple

from workflow.contracts import ValidationResult
from .state import StateStore
from .storage import utc_now_iso
from .validation import HandoffValidator


class PacketExecutor(Protocol):
    operations: list[str]

    def place_packet(self, destination_key: str, packet: Dict[str, Any]) -> Tuple[bool, str, str]: ...


class WorkflowEngine:
    def __init__(self, root: Path, manifest: Dict[str, Any], executor: PacketExecutor):
        self.root = root
        self.manifest = manifest
        workspace = root / "artifacts" / "sample_workspace"
        self.store = StateStore(workspace / "state.json", manifest)
        self.receipt_path = workspace / "receipts" / "receipts.jsonl"
        self.executor = executor
        approved_inbox = root / "artifacts" / manifest["inbox"]
        self.validator = HandoffValidator(manifest, approved_inbox)

    def _receipt(self, record: Dict[str, Any]) -> None:
        old_state = record.get("old_state") or {}
        new_state = record.get("new_state") or {}
        result = str(record.get("result", ""))
        validation_result = str(record.get("validation_result", ""))
        record.setdefault("project_id", self.manifest.get("project_id"))
        record.setdefault("receipt_id", str(uuid.uuid4()))
        record.setdefault("created_at", utc_now_iso())
        record.setdefault("state_revision_before", old_state.get("state_revision"))
        record.setdefault("state_revision_after", new_state.get("state_revision"))
        record.setdefault("approval_consumption_state", new_state.get("approval_state"))
        record.setdefault("reason_code", validation_result or result or None)
        if result == "REJECTED" or validation_result.startswith(("wrong-", "unexpected-", "stale-", "replay-", "invalid-", "duplicate-", "missing-", "unsupported-", "source-", "zip-", "malformed-")):
            record.setdefault("validation_decision", "DENY")
        else:
            record.setdefault("validation_decision", "ALLOW")
        record.setdefault("schema_version", "orbit.delivery-receipt/0.1-draft")
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with self.receipt_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    @staticmethod
    def _transition_key(sender: str, recipient: str) -> str:
        return f"{sender}->{recipient}"

    @staticmethod
    def _stable_id(prefix: str, *parts: Any) -> str:
        payload = "|".join(str(part) for part in parts).encode("utf-8")
        return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"

    def _route_decision_id(self, meta: Dict[str, Any], destination_key: str) -> str:
        return self._stable_id(
            "route",
            self.manifest.get("project_id"),
            self.manifest["workflow_id"],
            self.manifest["work_item"],
            meta.get("handoff_id"),
            meta.get("sequence"),
            meta.get("sender"),
            destination_key,
        )

    def _executor_idempotency_key(self, meta: Dict[str, Any], destination_key: str) -> str:
        return self._stable_id(
            "exec",
            self.manifest.get("project_id"),
            self.manifest["workflow_id"],
            self.manifest["work_item"],
            meta.get("handoff_id"),
            meta.get("artifact_digest"),
            destination_key,
        )

    def _approval_matches(self, approval: Dict[str, Any], context: Dict[str, Any]) -> bool:
        return (
            not approval.get("consumed", False)
            and approval.get("workflow_id") == self.manifest["workflow_id"]
            and approval.get("work_item") == self.manifest["work_item"]
            and approval.get("transition") == self._transition_key(context["sender"], context["recipient"])
            and approval.get("handoff_id") == context["handoff_id"]
            and approval.get("artifact_digest") == context["artifact_digest"]
        )

    def _find_matching_approval(self, state: Dict[str, Any], context: Dict[str, Any]) -> Optional[str]:
        for approval_id, approval in state.get("approval_records", {}).items():
            if self._approval_matches(approval, context):
                return approval_id
        return None

    def _consume_approval(self, state: Dict[str, Any], approval_id: str, handoff_id: str) -> None:
        approval = state["approval_records"][approval_id]
        approval["consumed"] = True
        approval["consumed_for_handoff_id"] = handoff_id
        approval["consumed_at"] = utc_now_iso()
        state["approval_state"] = "CONSUMED"
        state["pending_approval"] = None

    def _apply_complete_state(self, state: Dict[str, Any], meta: Dict[str, Any]) -> None:
        state["current_stage"] = meta["recipient"]
        state["current_owner_role"] = meta["recipient"]
        state["work_state"] = "COMPLETE" if meta["recipient"] == self.manifest["stages"][-1] else "READY_FOR_REVIEW"
        state["blocker_state"] = None

    def _delivery_packet(self, meta: Dict[str, Any], destination_key: str) -> Dict[str, Any]:
        route_id = self._route_decision_id(meta, destination_key)
        idempotency_key = self._executor_idempotency_key(meta, destination_key)
        return {
            "project_id": self.manifest["project_id"],
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "handoff_id": meta["handoff_id"],
            "sequence": meta["sequence"],
            "artifact_digest": meta["artifact_digest"],
            "status": meta["status"],
            "from": meta["sender"],
            "to": destination_key,
            "operation": "PLACE_PACKET",
            "route_decision_id": route_id,
            "requested_by_transition_id": route_id,
            "idempotency_key": idempotency_key,
            "schema_version": "orbit.executor-request/0.1-draft",
        }

    def _deliver(self, state: Dict[str, Any], meta: Dict[str, Any], destination_key: str) -> Tuple[str, str]:
        packet = self._delivery_packet(meta, destination_key)
        state["delivery_state"] = "ROUTING"
        ok, result, destination = self.executor.place_packet(destination_key, packet)
        if ok:
            state["delivery_state"] = "DELIVERED"
            state["pending_delivery"] = None
        else:
            state["delivery_state"] = result.split(":", 1)[0]
            state["pending_delivery"] = packet if state["delivery_state"] == "FAILED_RETRYABLE" else None
        return result, destination

    def _rejected_receipt(self, old_state: Dict[str, Any], state: Dict[str, Any], validation: ValidationResult) -> Dict[str, Any]:
        meta = validation.metadata or {}
        receipt = {
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "handoff_id": meta.get("handoff_id"),
            "sequence": meta.get("sequence"),
            "artifact_digest": meta.get("artifact_digest"),
            "accepted_artifact_digest": meta.get("accepted_artifact_digest"),
            "validation_result": validation.reason,
            "old_state": old_state,
            "transition": "NONE",
            "destination": "NONE",
            "adapter_type": "none",
            "result": "REJECTED",
            "new_state": state,
            "retryable": False,
        }
        self._receipt(receipt)
        return receipt

    def process(self, path: Path) -> Dict[str, Any]:
        state = self.store.load()
        old_state = copy.deepcopy(state)
        validation = self.validator.validate(path, state)
        if not validation.ok:
            return self._rejected_receipt(old_state, state, validation)

        assert validation.metadata is not None
        meta = validation.metadata
        handoff_id = meta["handoff_id"]
        status = meta["status"]
        transition_key = self._transition_key(meta["sender"], meta["recipient"])

        # Persist accepted identity before any external delivery.
        state["last_handoff_id"] = handoff_id
        state["last_artifact_digest"] = meta["artifact_digest"]
        state["last_sequence"] = meta["sequence"]
        state["accepted_handoff_ids"] = state.get("accepted_handoff_ids", []) + [handoff_id]
        state.setdefault("accepted_handoff_digests", {})[handoff_id] = meta["artifact_digest"]

        destination_key = ""
        approval_id: Optional[str] = None

        if status == "COMPLETE":
            if transition_key in self.manifest.get("approval_required_transitions", []):
                approval_id = self._find_matching_approval(state, meta)
                if approval_id is None:
                    state["approval_state"] = "PENDING"
                    state["pending_approval"] = {
                        "workflow_id": self.manifest["workflow_id"],
                        "work_item": self.manifest["work_item"],
                        "transition": transition_key,
                        "handoff_id": handoff_id,
                        "sequence": meta["sequence"],
                        "sender": meta["sender"],
                        "recipient": meta["recipient"],
                        "status": meta["status"],
                        "artifact_digest": meta["artifact_digest"],
                        "source_path": meta["source_path"],
                    }
                    state["delivery_state"] = "APPROVAL_PENDING"
                    state["pending_delivery"] = None
                    state["work_state"] = "READY_FOR_REVIEW"
                    self.store.save(state)
                    receipt = {
                        "workflow_id": self.manifest["workflow_id"],
                        "work_item": self.manifest["work_item"],
                        "handoff_id": handoff_id,
                        "sequence": meta["sequence"],
                        "artifact_digest": meta["artifact_digest"],
                        "approval_id": None,
                        "validation_result": "accepted",
                        "old_state": old_state,
                        "transition": f"APPROVAL_PENDING:{transition_key}",
                        "destination": "NONE",
                        "adapter_type": "none",
                        "result": "APPROVAL_PENDING",
                        "new_state": state,
                        "retryable": False,
                    }
                    self._receipt(receipt)
                    return receipt
                self._consume_approval(state, approval_id, handoff_id)

            self._apply_complete_state(state, meta)
            destination_key = meta["recipient"]
        elif status == "BLOCKED":
            state["work_state"] = "BLOCKED"
            state["blocker_state"] = {"handoff_id": handoff_id, "owner": meta["sender"], "status": "OPEN"}
            destination_key = "BLOCKER"
        elif status == "NEEDS_DECISION":
            state["work_state"] = "NEEDS_DECISION"
            state["blocker_state"] = {"handoff_id": handoff_id, "owner": meta["sender"], "status": "NEEDS_DECISION"}
            destination_key = "DECISION"
        elif status in {"REQUEST_CHANGES", "REQUEST_WORKER"}:
            state["work_state"] = "REQUEST_CHANGES"
            state["blocker_state"] = {"handoff_id": handoff_id, "owner": meta["sender"], "status": status}
            destination_key = "WORKER"

        result, destination = self._deliver(state, meta, destination_key)
        self.store.save(state)

        receipt = {
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "handoff_id": handoff_id,
            "sequence": meta["sequence"],
            "artifact_digest": meta["artifact_digest"],
            "approval_id": approval_id,
            "validation_result": "accepted",
            "old_state": old_state,
            "transition": status if approval_id is None else f"APPROVED_COMPLETE:{transition_key}",
            "route_decision_id": self._route_decision_id(meta, destination_key),
            "executor_idempotency_key": self._executor_idempotency_key(meta, destination_key),
            "destination": destination,
            "adapter_type": "local-place-packet",
            "result": result,
            "new_state": state,
            "retryable": result.startswith("FAILED_RETRYABLE"),
        }
        self._receipt(receipt)
        return receipt

    def register_approval(self, approval: Dict[str, Any]) -> Dict[str, Any]:
        state = self.store.load()
        old_state = copy.deepcopy(state)
        required = ["approval_id", "workflow_id", "work_item", "transition", "handoff_id", "artifact_digest"]
        missing = [key for key in required if not approval.get(key)]
        if missing:
            return self._reject_approval(old_state, state, approval, "invalid-approval:missing-" + ",".join(missing))

        normalized = {key: str(approval[key]) for key in required}
        normalized["approved_by_principal"] = str(approval.get("approved_by_principal", "test-authority"))
        normalized["authority_ref"] = str(approval.get("authority_ref", "configured-test-authority"))
        normalized["decision"] = str(approval.get("decision", "APPROVE")).upper()
        normalized["decided_at"] = str(approval.get("decided_at", utc_now_iso()))
        normalized["consumed"] = False
        normalized["schema_version"] = "orbit.approval-record/0.1-draft"

        if normalized["decision"] != "APPROVE":
            return self._reject_approval(old_state, state, normalized, "invalid-approval:decision-not-approve")
        if normalized["workflow_id"] != self.manifest["workflow_id"]:
            return self._reject_approval(old_state, state, normalized, "invalid-approval:wrong-workflow")
        if normalized["work_item"] != self.manifest["work_item"]:
            return self._reject_approval(old_state, state, normalized, "invalid-approval:wrong-work-item")
        if normalized["transition"] not in self.manifest.get("approval_required_transitions", []):
            return self._reject_approval(old_state, state, normalized, "invalid-approval:transition-not-gated")

        records = state.setdefault("approval_records", {})
        approval_id = normalized["approval_id"]
        if approval_id in records:
            existing = records[approval_id]
            same_contract = all(existing.get(k) == normalized.get(k) for k in required)
            return self._reject_approval(old_state, state, normalized, "duplicate-approval" if same_contract else "approval-id-conflict")

        for existing in records.values():
            if (
                existing.get("consumed")
                and existing.get("workflow_id") == normalized["workflow_id"]
                and existing.get("work_item") == normalized["work_item"]
                and existing.get("transition") == normalized["transition"]
                and existing.get("handoff_id") == normalized["handoff_id"]
                and existing.get("artifact_digest") == normalized["artifact_digest"]
            ):
                return self._reject_approval(old_state, state, normalized, "approval-context-already-consumed")

        records[approval_id] = normalized
        pending = state.get("pending_approval")
        if pending and self._approval_matches(normalized, pending):
            self._consume_approval(state, approval_id, pending["handoff_id"])
            self._apply_complete_state(state, pending)
            result, destination = self._deliver(state, pending, pending["recipient"])
            self.store.save(state)
            receipt = {
                "workflow_id": self.manifest["workflow_id"],
                "work_item": self.manifest["work_item"],
                "handoff_id": pending["handoff_id"],
                "sequence": pending["sequence"],
                "artifact_digest": pending["artifact_digest"],
                "approval_id": approval_id,
                "validation_result": "approval-valid",
                "old_state": old_state,
                "transition": f"APPROVED_COMPLETE:{pending['transition']}",
                "route_decision_id": self._route_decision_id(pending, pending["recipient"]),
                "executor_idempotency_key": self._executor_idempotency_key(pending, pending["recipient"]),
                "destination": destination,
                "adapter_type": "local-place-packet",
                "result": result,
                "new_state": state,
                "retryable": result.startswith("FAILED_RETRYABLE"),
            }
            self._receipt(receipt)
            return receipt

        state["approval_state"] = "APPROVED" if not pending else "PENDING"
        self.store.save(state)
        receipt = {
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "handoff_id": normalized["handoff_id"],
            "artifact_digest": normalized["artifact_digest"],
            "approval_id": approval_id,
            "validation_result": "approval-valid",
            "old_state": old_state,
            "transition": "APPROVAL_PERSISTED",
            "destination": "NONE",
            "adapter_type": "none",
            "result": "APPROVAL_PERSISTED",
            "new_state": state,
            "retryable": False,
        }
        self._receipt(receipt)
        return receipt

    def _reject_approval(self, old_state: Dict[str, Any], state: Dict[str, Any], approval: Dict[str, Any], reason: str) -> Dict[str, Any]:
        receipt = {
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "handoff_id": approval.get("handoff_id"),
            "artifact_digest": approval.get("artifact_digest"),
            "approval_id": approval.get("approval_id"),
            "validation_result": reason,
            "old_state": old_state,
            "transition": "APPROVAL_NONE",
            "destination": "NONE",
            "adapter_type": "none",
            "result": "REJECTED",
            "new_state": state,
            "retryable": False,
        }
        self._receipt(receipt)
        return receipt

    def retry_pending(self) -> Optional[Dict[str, Any]]:
        state = self.store.load()
        packet = state.get("pending_delivery")
        if not packet:
            return None
        old_state = copy.deepcopy(state)
        ok, result, destination = self.executor.place_packet(packet["to"], packet)
        state["delivery_state"] = "DELIVERED" if ok else result.split(":", 1)[0]
        if ok:
            state["pending_delivery"] = None
        self.store.save(state)
        record = {
            "workflow_id": self.manifest["workflow_id"],
            "work_item": self.manifest["work_item"],
            "handoff_id": packet["handoff_id"],
            "sequence": packet["sequence"],
            "artifact_digest": packet["artifact_digest"],
            "validation_result": "already-accepted",
            "old_state": old_state,
            "transition": "DELIVERY_RETRY",
            "route_decision_id": packet.get("route_decision_id"),
            "executor_idempotency_key": packet.get("idempotency_key"),
            "destination": destination,
            "adapter_type": "local-place-packet",
            "result": result,
            "new_state": state,
            "retryable": result.startswith("FAILED_RETRYABLE"),
        }
        self._receipt(record)
        return record
