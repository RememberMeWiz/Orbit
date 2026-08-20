"""Isolated work-item lane state and persistence for Orbit Multi-Work-Item Supervisor.

Guarantees:
- Multiple work items do not share mutable workflow state.
- One work item never consumes another work item's PM directive.
- One lane blocking does not freeze unrelated safe lanes.
- Each lane has its own dedicated directory, PM bridge state, delivery ledger,
  teaching traces, inbox, and stop control.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from workflow.core.storage import atomic_write_json

from ..bridge.delivery import DeliveryLedger
from ..bridge.orchestrator import ApprenticeLoop
from ..bridge.pm_envelope import PMBridgeState
from ..bridge.teaching import TeachingTraceStore


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Standard Work States
STATE_INITIALIZED = "INITIALIZED"
STATE_AWAITING_PM_ROUTING = "AWAITING_PM_ROUTING"
STATE_DIRECTIVE_ACCEPTED = "DIRECTIVE_ACCEPTED"
STATE_DISPATCHING = "DISPATCHING"
STATE_AWAITING_WORKER = "AWAITING_WORKER"
STATE_COLLECTING = "COLLECTING"
STATE_REPORTING_TO_PM = "REPORTING_TO_PM"
STATE_COMPLETED = "COMPLETED"
STATE_PAUSED = "PAUSED"
STATE_BLOCKED = "BLOCKED"
STATE_STOPPED = "STOPPED"


@dataclass
class LaneRecord:
    work_item: str
    objective: str
    current_endpoint: str = ""
    pending_request_id: str = ""
    accepted_directive_id: str = ""
    expected_handoff: str = ""
    expected_sender: str = ""
    delivery_record_id: str = ""
    work_state: str = STATE_INITIALIZED
    last_observed_state: Dict[str, Any] = field(default_factory=dict)
    last_progress_at: str = field(default_factory=utc_now_iso)
    created_at: str = field(default_factory=utc_now_iso)
    blocker_code: str = ""
    blocker_detail: str = ""
    result_digest: str = ""
    assignment_path: str = ""
    artifact_path: str = ""
    verify_token: str = ""
    source: str = "transcript"
    nonce: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LaneRecord":
        fields = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in fields}
        return cls(**filtered)


class WorkItemLane:
    """Encapsulates durable execution state for a single independent work item."""

    def __init__(self, lane_dir: Path, global_stop_path: Optional[Path] = None):
        self.lane_dir = Path(lane_dir)
        self.lane_dir.mkdir(parents=True, exist_ok=True)
        self.record_path = self.lane_dir / "lane.json"
        self.stop_path = self.lane_dir / "STOP"
        self.global_stop_path = Path(global_stop_path) if global_stop_path else None
        self.pm_state_path = self.lane_dir / "pm_bridge.json"
        self.delivery_path = self.lane_dir / "delivery.json"
        self.traces_path = self.lane_dir / "teaching_traces.jsonl"
        self.inbox_dir = self.lane_dir / "inbox"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        self.work_item = self.lane_dir.name
        self.record = self._load_or_create()

    def _load_or_create(self) -> LaneRecord:
        if self.record_path.exists():
            try:
                data = json.loads(self.record_path.read_text(encoding="utf-8"))
                return LaneRecord.from_dict(data)
            except Exception:
                pass
        rec = LaneRecord(work_item=self.work_item, objective="")
        self.save_record(rec)
        return rec

    def load_record(self) -> LaneRecord:
        if self.record_path.exists():
            data = json.loads(self.record_path.read_text(encoding="utf-8"))
            self.record = LaneRecord.from_dict(data)
        return self.record

    def save_record(self, record: Optional[LaneRecord] = None) -> None:
        if record is not None:
            self.record = record
        self.record.last_progress_at = utc_now_iso()
        atomic_write_json(self.record_path, self.record.to_dict())

    def stopped(self) -> bool:
        return bool(
            (self.stop_path and self.stop_path.is_file())
            or (self.global_stop_path and self.global_stop_path.is_file())
            or self.record.work_state == STATE_STOPPED
        )

    def paused(self) -> bool:
        return self.record.work_state == STATE_PAUSED

    def pause(self) -> None:
        self.record.work_state = STATE_PAUSED
        self.save_record()

    def resume(self) -> None:
        if self.record.work_state == STATE_PAUSED:
            self.record.work_state = (
                STATE_AWAITING_PM_ROUTING
                if self.record.pending_request_id
                else STATE_INITIALIZED
            )
            self.save_record()

    def stop(self) -> None:
        self.stop_path.touch(exist_ok=True)
        self.record.work_state = STATE_STOPPED
        self.save_record()

    def build_loop(self, adapter) -> ApprenticeLoop:
        """Create an ApprenticeLoop strictly isolated to this lane's directory."""
        return ApprenticeLoop(
            adapter=adapter,
            pm_state=PMBridgeState(self.pm_state_path, work_item=self.work_item),
            ledger=DeliveryLedger(self.delivery_path, work_item=self.work_item),
            traces=TeachingTraceStore(self.traces_path, work_item=self.work_item),
            work_item=self.work_item,
            inbox_dir=self.inbox_dir,
            stop_path=self.stop_path,
        )

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "work_item": self.work_item,
            "objective": self.record.objective,
            "work_state": self.record.work_state,
            "current_endpoint": self.record.current_endpoint,
            "pending_request_id": self.record.pending_request_id,
            "accepted_directive_id": self.record.accepted_directive_id,
            "last_progress_at": self.record.last_progress_at,
            "stopped": self.stopped(),
            "blocker": self.record.blocker_code,
        }
