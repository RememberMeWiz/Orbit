"""Workflow speed, latency, and efficiency instrumentation for Orbit.

Records per-lane and per-hop telemetry to measure:
- PM decision wait time
- Dispatch latency
- Worker response time
- Collection/validation time
- Total hop wall-clock time
- Number of retries
- Number of PM interruptions
- Number of human UI actions
- Number of courier actions
- Number of paid work-mode escalations
- Failure/blocker reasons

Durable append-only JSONL format per state directory.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class HopTelemetry:
    work_item: str
    hop_id: str
    target_endpoint: str
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str = ""
    pm_request_id: str = ""
    directive_id: str = ""
    pm_wait_seconds: float = 0.0
    dispatch_seconds: float = 0.0
    worker_response_seconds: float = 0.0
    collect_seconds: float = 0.0
    total_hop_seconds: float = 0.0
    retries: int = 0
    pm_interruptions: int = 1
    human_ui_actions: int = 0
    courier_actions: int = 0
    work_mode_escalations: int = 0
    result: str = "SUCCESS"
    blocker_reason: str = ""
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HopTelemetry":
        fields = set(cls.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in fields}
        return cls(**filtered)


class TelemetryStore:
    """Durable append-only telemetry storage and aggregator."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, record: HopTelemetry) -> None:
        if not record.completed_at:
            record.completed_at = utc_now_iso()
        if record.total_hop_seconds <= 0.0:
            record.total_hop_seconds = (
                record.pm_wait_seconds
                + record.dispatch_seconds
                + record.worker_response_seconds
                + record.collect_seconds
            )
        line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)

    def all_records(self) -> List[HopTelemetry]:
        if not self.path.exists():
            return []
        records: List[HopTelemetry] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(HopTelemetry.from_dict(json.loads(line)))
                except Exception:
                    continue
        return records

    def summary(self) -> Dict[str, Any]:
        records = self.all_records()
        total_hops = len(records)
        if total_hops == 0:
            return {
                "total_hops": 0,
                "successful_hops": 0,
                "blocked_hops": 0,
                "total_wall_clock_seconds": 0.0,
                "avg_pm_wait_seconds": 0.0,
                "median_pm_wait_seconds": 0.0,
                "avg_dispatch_seconds": 0.0,
                "avg_worker_response_seconds": 0.0,
                "avg_collect_seconds": 0.0,
                "avg_total_hop_seconds": 0.0,
                "total_human_ui_actions": 0,
                "total_courier_actions": 0,
                "total_work_mode_escalations": 0,
                "zero_courier_rate": 1.0,
                "zero_click_rate": 1.0,
                "endpoint_counts": {},
                "blocker_reasons": {},
            }

        successful = [r for r in records if r.result == "SUCCESS"]
        blocked = [r for r in records if r.result in ("BLOCKED", "FAILED")]

        pm_waits = [r.pm_wait_seconds for r in records if r.pm_wait_seconds > 0]
        dispatches = [r.dispatch_seconds for r in records if r.dispatch_seconds > 0]
        worker_responses = [r.worker_response_seconds for r in records if r.worker_response_seconds > 0]
        collects = [r.collect_seconds for r in records if r.collect_seconds > 0]
        totals = [r.total_hop_seconds for r in records if r.total_hop_seconds > 0]

        total_human_clicks = sum(r.human_ui_actions for r in records)
        total_couriers = sum(r.courier_actions for r in records)
        total_work_modes = sum(r.work_mode_escalations for r in records)

        zero_couriers = sum(1 for r in records if r.courier_actions == 0)
        zero_clicks = sum(1 for r in records if r.human_ui_actions == 0)

        endpoint_counts: Dict[str, int] = {}
        blocker_reasons: Dict[str, int] = {}
        for r in records:
            ep = r.target_endpoint or "unknown"
            endpoint_counts[ep] = endpoint_counts.get(ep, 0) + 1
            if r.blocker_reason:
                blocker_reasons[r.blocker_reason] = blocker_reasons.get(r.blocker_reason, 0) + 1

        return {
            "total_hops": total_hops,
            "successful_hops": len(successful),
            "blocked_hops": len(blocked),
            "total_wall_clock_seconds": round(sum(totals), 2),
            "avg_pm_wait_seconds": round(statistics.mean(pm_waits), 2) if pm_waits else 0.0,
            "median_pm_wait_seconds": round(statistics.median(pm_waits), 2) if pm_waits else 0.0,
            "avg_dispatch_seconds": round(statistics.mean(dispatches), 2) if dispatches else 0.0,
            "avg_worker_response_seconds": round(statistics.mean(worker_responses), 2) if worker_responses else 0.0,
            "avg_collect_seconds": round(statistics.mean(collects), 2) if collects else 0.0,
            "avg_total_hop_seconds": round(statistics.mean(totals), 2) if totals else 0.0,
            "total_human_ui_actions": total_human_clicks,
            "total_courier_actions": total_couriers,
            "total_work_mode_escalations": total_work_modes,
            "zero_courier_rate": round(zero_couriers / total_hops, 3),
            "zero_click_rate": round(zero_clicks / total_hops, 3),
            "endpoint_counts": endpoint_counts,
            "blocker_reasons": blocker_reasons,
        }

    def format_report(self) -> str:
        s = self.summary()
        lines = [
            "============================================================",
            "                ORBIT WORKFLOW EFFICIENCY METRICS           ",
            "============================================================",
            f"Total Workflow Hops        : {s['total_hops']}",
            f"  Successful Hops          : {s['successful_hops']}",
            f"  Blocked / Failed Hops    : {s['blocked_hops']}",
            f"Total Wall-Clock Time      : {s['total_wall_clock_seconds']:.1f}s",
            "------------------------------------------------------------",
            "Latency Breakdown (Averages & Medians):",
            f"  PM Decision Wait (Avg)   : {s['avg_pm_wait_seconds']:.1f}s (Median: {s['median_pm_wait_seconds']:.1f}s)",
            f"  Dispatch Latency (Avg)   : {s['avg_dispatch_seconds']:.1f}s",
            f"  Worker Response (Avg)    : {s['avg_worker_response_seconds']:.1f}s",
            f"  Collection Time (Avg)    : {s['avg_collect_seconds']:.1f}s",
            f"  Total Hop Duration (Avg) : {s['avg_total_hop_seconds']:.1f}s",
            "------------------------------------------------------------",
            "Autonomy & Efficiency Ratios:",
            f"  Zero-Courier Rate        : {s['zero_courier_rate']*100:.1f}% (Courier actions: {s['total_courier_actions']})",
            f"  Zero-Click Rate          : {s['zero_click_rate']*100:.1f}% (Human UI clicks: {s['total_human_ui_actions']})",
            f"  Work-Mode Escalations    : {s['total_work_mode_escalations']}",
        ]
        if s["endpoint_counts"]:
            lines.append("------------------------------------------------------------")
            lines.append("Target Endpoint Distribution:")
            for ep, count in sorted(s["endpoint_counts"].items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {ep:<24} : {count} hops")
        if s["blocker_reasons"]:
            lines.append("------------------------------------------------------------")
            lines.append("Observed Blockers:")
            for reason, count in sorted(s["blocker_reasons"].items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {reason:<24} : {count}")
        lines.append("============================================================")
        return "\n".join(lines)
