"""Teaching traces: what PM decided, what Orbit did, what happened.

Append-only. Each trace records one PM-supervised decision so repeated patterns
become data later.

The hard rule is in ``propose_promotion``: Orbit may *observe* that PM has made
the same call under equivalent conditions N times, and may say so. It may not
turn that observation into a policy. Promotion to autonomous behaviour requires
explicit PM/Architecture approval, and nothing in this module can grant it --
the function returns a proposal, and there is deliberately no ``promote()``.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from workflow.core.storage import utc_now_iso

TRACE_SCHEMA = "orbit.teaching-trace/0.1-draft"

# Redacted before write. A teaching corpus must never become a credential store.
_SECRET_FRAGMENTS = ("token", "secret", "credential", "password", "apikey", "api_key", "authorization", "cookie", "session_key")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            folded = str(key).lower().replace("-", "").replace("_", "")
            if any(f.replace("_", "") in folded for f in _SECRET_FRAGMENTS):
                out[key] = "[REDACTED]"
            else:
                out[key] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


@dataclass(frozen=True)
class TeachingTrace:
    work_item: str
    pm_request_id: str
    directive_id: str
    action: str
    condition_digest: str
    state_before: Dict[str, Any] = field(default_factory=dict)
    state_after: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    result: str = ""
    classification: str = ""
    trace_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return _redact({
            "schema_version": TRACE_SCHEMA,
            "trace_id": self.trace_id or str(uuid.uuid4()),
            "created_at": utc_now_iso(),
            "work_item": self.work_item,
            "pm_request_id": self.pm_request_id,
            "directive_id": self.directive_id,
            "action": self.action,
            "condition_digest": self.condition_digest,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "evidence": self.evidence,
            "result": self.result,
            "classification": self.classification,
        })


def condition_digest(*, work_item: str, owner: str, work_state: str, reason: str) -> str:
    """Stable fingerprint of the situation a decision was made in.

    Two decisions share a digest only when the work item, owning role, workflow
    state and triggering reason all match -- which is what "equivalent
    conditions" has to mean before any pattern claim is honest.
    """
    payload = "|".join([work_item, owner, work_state, reason]).encode("utf-8")
    return "cond-" + hashlib.sha256(payload).hexdigest()[:20]


class TeachingTraceStore:
    def __init__(self, path: Path, *, work_item: str):
        self.path = Path(path)
        self.work_item = work_item

    def append(self, trace: TeachingTrace) -> Dict[str, Any]:
        if trace.work_item != self.work_item:
            raise ValueError("teaching-trace-work-item-mismatch")
        record = trace.to_dict()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def propose_promotion(self, *, threshold: int = 3) -> List[Dict[str, Any]]:
        """Report repeated PM decisions. Reporting only -- never promotion.

        The returned proposals are advisory records for a human to act on. There
        is no corresponding method that makes one active, and nothing in Orbit
        consults these to decide behaviour.
        """
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for record in self.all():
            key = f"{record.get('condition_digest')}|{record.get('action')}"
            buckets.setdefault(key, []).append(record)

        proposals = []
        for key, records in sorted(buckets.items()):
            if len(records) < threshold:
                continue
            condition, action = key.split("|", 1)
            proposals.append({
                "status": "PROPOSAL_ONLY",
                "requires": "explicit PM/Architecture approval",
                "condition_digest": condition,
                "action": action,
                "observed_count": len(records),
                "trace_ids": [r.get("trace_id") for r in records],
                "note": f"PM chose {action} {len(records)} times under equivalent conditions. "
                        "This is an observation, not an authorisation.",
            })
        return proposals
