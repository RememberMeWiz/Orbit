"""Workflow self-improvement insights engine for Orbit.

Analyzes historical telemetry and teaching traces to surface workflow
bottlenecks without modifying execution policies or expanding authority.

Allowed examples:
- "Median PM wait is 14m; consider batching decisions."
- "Two lanes repeatedly blocked on the same endpoint."
- "Transcript collection saved N file-mode escalations."

All improvements remain proposals/evidence for PM or an operator to act on.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .telemetry import TelemetryStore


@dataclass(frozen=True)
class WorkflowInsight:
    category: str  # LATENCY, RELIABILITY, EFFICIENCY, BOTTLENECK
    observation: str
    proposal: str
    impact: str  # HIGH, MEDIUM, LOW

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "observation": self.observation,
            "proposal": self.proposal,
            "impact": self.impact,
        }


class WorkflowInsightsAnalyzer:
    """Conservative analysis of workflow traces and metrics."""

    def __init__(self, telemetry_store: TelemetryStore, traces_dir: Optional[Path] = None):
        self.telemetry = telemetry_store
        self.traces_dir = Path(traces_dir) if traces_dir else None

    def analyze(self) -> List[WorkflowInsight]:
        insights: List[WorkflowInsight] = []
        summary = self.telemetry.summary()
        records = self.telemetry.all_records()

        total_hops = summary.get("total_hops", 0)
        if total_hops == 0:
            return [
                WorkflowInsight(
                    category="EFFICIENCY",
                    observation="No workflow hops recorded yet.",
                    proposal="Run overnight supervisor to gather operational traces.",
                    impact="LOW",
                )
            ]

        # 1. PM Decision Latency Analysis
        median_pm_wait = summary.get("median_pm_wait_seconds", 0.0)
        if median_pm_wait > 600.0:  # > 10 minutes
            mins = round(median_pm_wait / 60.0, 1)
            insights.append(
                WorkflowInsight(
                    category="LATENCY",
                    observation=f"Median PM decision wait is {mins} minutes ({median_pm_wait:.0f}s).",
                    proposal="Consider batching multi-hop assignments or preparing pre-approved routing envelopes.",
                    impact="HIGH" if median_pm_wait > 1200.0 else "MEDIUM",
                )
            )
        elif median_pm_wait > 0.0 and median_pm_wait < 60.0:
            insights.append(
                WorkflowInsight(
                    category="LATENCY",
                    observation=f"Fast PM responsiveness observed: median decision wait is {median_pm_wait:.1f}s.",
                    proposal="Maintain responsive PM envelope loop for rapid round-trips.",
                    impact="LOW",
                )
            )

        # 2. Courier & Paid Work-Mode Escalation Analysis
        zero_courier_rate = summary.get("zero_courier_rate", 1.0)
        work_mode_escalations = summary.get("total_work_mode_escalations", 0)
        transcript_hops = sum(1 for r in records if r.courier_actions == 0 and r.work_mode_escalations == 0)

        if transcript_hops > 0:
            insights.append(
                WorkflowInsight(
                    category="EFFICIENCY",
                    observation=f"Transcript collection satisfied {transcript_hops} handoff(s) without paid work-mode prompts or manual couriers.",
                    proposal="Keep transcript collection as default source for all text handoffs.",
                    impact="MEDIUM",
                )
            )

        if work_mode_escalations > 0:
            insights.append(
                WorkflowInsight(
                    category="EFFICIENCY",
                    observation=f"Observed {work_mode_escalations} paid work-mode escalation(s).",
                    proposal="Ensure workers format text handoffs directly in conversation rather than generating downloadable card attachments.",
                    impact="MEDIUM",
                )
            )

        # 3. Endpoint Reliability & Bottleneck Analysis
        endpoint_blockers: Dict[str, int] = {}
        for r in records:
            if r.result in ("BLOCKED", "FAILED"):
                ep = r.target_endpoint or "unknown"
                endpoint_blockers[ep] = endpoint_blockers.get(ep, 0) + 1

        for ep, count in endpoint_blockers.items():
            if count >= 2:
                insights.append(
                    WorkflowInsight(
                        category="BOTTLENECK",
                        observation=f"Endpoint '{ep}' encountered {count} blocker/failure events.",
                        proposal=f"Inspect prompt instructions and handoff schemas for role '{ep}' to reduce ambiguity.",
                        impact="HIGH",
                    )
                )

        # 4. Multi-Hop Duration vs Worker Response
        avg_worker_resp = summary.get("avg_worker_response_seconds", 0.0)
        avg_dispatch = summary.get("avg_dispatch_seconds", 0.0)
        if avg_dispatch > 30.0:
            insights.append(
                WorkflowInsight(
                    category="LATENCY",
                    observation=f"Average dispatch staging latency is {avg_dispatch:.1f}s.",
                    proposal="Verify named-mutex contention and UIA selector caching to streamline dispatch.",
                    impact="LOW",
                )
            )

        return insights

    def format_insights(self) -> str:
        insights = self.analyze()
        lines = [
            "============================================================",
            "             ORBIT WORKFLOW SELF-IMPROVEMENT INSIGHTS       ",
            "============================================================",
        ]
        for i, item in enumerate(insights, 1):
            lines.append(f"[{i}] [{item.category}] (Impact: {item.impact})")
            lines.append(f"    Observation : {item.observation}")
            lines.append(f"    Proposal    : {item.proposal}")
            lines.append("")
        lines.append("Note: Orbit never automatically alters routing rules or execution authority.")
        lines.append("============================================================")
        return "\n".join(lines)
