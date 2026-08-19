"""Persisted transport state and append-only transport receipts.

Restart safety is the whole point of this module. A submitted request is
recorded *before* the external agent is started, so a crash between "spawn" and
"record" can only ever leave a request that looks submitted when it was not --
which reconciles safely -- never one that looks fresh when an external task is
already running.
"""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from workflow.core.storage import atomic_write_json, utc_now_iso

from .contracts import TransportError, TransportRequest

TRANSPORT_STATE_SCHEMA = "orbit.transport-state/0.1-draft"
TRANSPORT_RECEIPT_SCHEMA = "orbit.transport-receipt/0.1-draft"

# Anything matching these key fragments is redacted before it can reach a
# receipt or persisted state. Session credentials must never become audit data.
_SECRET_KEY_FRAGMENTS = ("token", "secret", "credential", "password", "apikey", "api_key", "authorization")


def redact(value: Any) -> Any:
    """Recursively drop values whose key names look credential-bearing."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            folded = str(key).lower().replace("-", "").replace("_", "")
            if any(fragment.replace("_", "") in folded for fragment in _SECRET_KEY_FRAGMENTS):
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class TransportStore:
    def __init__(self, path: Path, *, project_id: str, workflow_id: str, work_item: str):
        self.path = path
        self.receipts_path = path.parent / "transport_receipts.jsonl"
        self.project_id = project_id
        self.workflow_id = workflow_id
        self.work_item = work_item

    def initial(self) -> Dict[str, Any]:
        return {
            "schema_version": TRANSPORT_STATE_SCHEMA,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "work_item": self.work_item,
            "requests": {},
            "state_revision": 0,
            "updated_at": utc_now_iso(),
        }

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            state = self.initial()
            self.save(state)
            return state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TransportError("transport-state-malformed") from exc
        if not isinstance(state, dict):
            raise TransportError("transport-state-not-object")

        # Transport state is bound to one work item. Opening another work item's
        # state would let a collected result be attributed to the wrong item.
        for key, expected in (
            ("project_id", self.project_id),
            ("workflow_id", self.workflow_id),
            ("work_item", self.work_item),
        ):
            actual = state.get(key)
            if actual is not None and actual != expected:
                raise TransportError(f"transport-state-{key}-mismatch")
        state.setdefault("requests", {})
        return state

    def save(self, state: Dict[str, Any]) -> None:
        state["state_revision"] = int(state.get("state_revision", 0)) + 1
        state["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, state)

    # -- request records -------------------------------------------------

    @staticmethod
    def record_for(state: Dict[str, Any], request: TransportRequest) -> Optional[Dict[str, Any]]:
        return state.get("requests", {}).get(request.request_id)

    def mark_submitted(
        self,
        state: Dict[str, Any],
        request: TransportRequest,
        *,
        correlation_id: str,
    ) -> Dict[str, Any]:
        records = state.setdefault("requests", {})
        existing = records.get(request.request_id)
        attempt = int(existing.get("attempt", 0)) + 1 if existing else 1
        record = {
            "request": request.to_dict(),
            "transport_state": "SUBMITTED",
            "correlation_id": correlation_id,
            "attempt": attempt,
            "submitted_at": utc_now_iso(),
            "collected_at": None,
            "result": None,
            "late_result": None,
        }
        records[request.request_id] = record
        self.save(state)
        return record

    def mark_result(
        self,
        state: Dict[str, Any],
        request: TransportRequest,
        result: Dict[str, Any],
        *,
        transport_state: str,
        late: bool = False,
    ) -> Dict[str, Any]:
        records = state.setdefault("requests", {})
        record = records.get(request.request_id)
        if record is None:
            raise TransportError("transport-record-missing")
        record["transport_state"] = transport_state
        if late:
            # A result that arrives after STOP is preserved as evidence but is
            # never promoted into the collected slot that drives advancement.
            record["late_result"] = redact(result)
        else:
            record["result"] = redact(result)
            record["collected_at"] = utc_now_iso()
        self.save(state)
        return record

    # -- receipts --------------------------------------------------------

    def append_receipt(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        receipt = dict(payload)
        receipt.setdefault("receipt_id", str(uuid.uuid4()))
        receipt.setdefault("created_at", utc_now_iso())
        receipt.setdefault("schema_version", TRANSPORT_RECEIPT_SCHEMA)
        receipt = redact(receipt)
        self.receipts_path.parent.mkdir(parents=True, exist_ok=True)
        with self.receipts_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, sort_keys=True) + "\n")
        return receipt

    def read_receipts(self) -> list:
        if not self.receipts_path.exists():
            return []
        records = []
        for line in self.receipts_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records


def snapshot(state: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(state)
