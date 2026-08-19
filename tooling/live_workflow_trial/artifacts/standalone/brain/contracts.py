"""Provider-neutral local reasoning contracts.

Orbit core calls this interface, never a vendor SDK. Nothing in this package
imports a network library, a vendor client, or any workflow state type -- that
last exclusion is structural, not stylistic: a brain result is *data*, and the
only way it can affect durable state is by being handed to governed
deterministic code that validates it first.

Two authority rules are enforced here rather than left to convention:

* a result may only claim capabilities the request already granted
  (``used_capabilities`` is checked against ``allowed_capabilities``);
* a result may never carry a directive to change permissions, scope or
  acceptance criteria (``_FORBIDDEN_RESULT_KEYS``).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Tuple


class BrainError(ValueError):
    """Raised when a brain request or result violates its contract."""


BRAIN_STATUSES: Tuple[str, ...] = (
    "OK",
    "BLOCKED",
    "NEEDS_DECISION",
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
)

# A model may describe work. It may not re-grant itself authority. Any of these
# appearing at the top level of a structured result is treated as an escalation
# attempt and rejected outright.
_FORBIDDEN_RESULT_KEYS = (
    "allowed_capabilities",
    "capabilities",
    "permissions",
    "allowed_executor_operations",
    "acceptance_criteria",
    "destinations",
    "role_destination_registry",
    "approval_required_transitions",
)


@dataclass(frozen=True)
class LocalBrainRequest:
    task_id: str
    role: str
    objective: str
    context: Dict[str, Any] = field(default_factory=dict)
    allowed_capabilities: Tuple[str, ...] = ()
    result_schema: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "role", "objective"):
            if not str(getattr(self, name) or "").strip():
                raise BrainError(f"brain-request-missing:{name}")
        if not isinstance(self.context, Mapping):
            raise BrainError("brain-request-context-not-mapping")
        if not isinstance(self.result_schema, Mapping):
            raise BrainError("brain-request-schema-not-mapping")

    @property
    def digest(self) -> str:
        payload = json.dumps(
            {
                "task_id": self.task_id,
                "role": self.role,
                "objective": self.objective,
                "context": self.context,
                "allowed_capabilities": list(self.allowed_capabilities),
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "objective": self.objective,
            "context": dict(self.context),
            "allowed_capabilities": list(self.allowed_capabilities),
            "result_schema": dict(self.result_schema),
            "request_digest": self.digest,
        }


@dataclass(frozen=True)
class LocalBrainResult:
    task_id: str
    status: str
    result: Dict[str, Any] = field(default_factory=dict)
    reason_code: str = ""
    detail: str = ""
    used_capabilities: Tuple[str, ...] = ()
    provider: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in BRAIN_STATUSES:
            raise BrainError(f"brain-status-not-allowlisted:{self.status}")
        if not isinstance(self.result, Mapping):
            raise BrainError("brain-result-not-mapping")

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "result": dict(self.result),
            "reason_code": self.reason_code,
            "detail": self.detail,
            "used_capabilities": list(self.used_capabilities),
            "provider": self.provider,
            "evidence": dict(self.evidence),
        }


def validate_result(request: LocalBrainRequest, result: LocalBrainResult) -> LocalBrainResult:
    """Gate a brain result before any governed code is allowed to act on it.

    Returns the result unchanged when it is well-formed, or a typed
    FAILED_FINAL result describing the violation. It never raises: a misbehaving
    model must degrade into a recorded failure, not an exception that unwinds
    workflow state.
    """
    if result.task_id != request.task_id:
        return LocalBrainResult(
            task_id=request.task_id,
            status="FAILED_FINAL",
            reason_code="brain-result-task-mismatch",
            detail=f"expected {request.task_id}, got {result.task_id}",
            provider=result.provider,
        )

    escalated = sorted(set(result.used_capabilities) - set(request.allowed_capabilities))
    if escalated:
        return LocalBrainResult(
            task_id=request.task_id,
            status="FAILED_FINAL",
            reason_code="brain-capability-escalation",
            detail="claimed capabilities not granted: " + ",".join(escalated),
            provider=result.provider,
        )

    present = [key for key in _FORBIDDEN_RESULT_KEYS if key in result.result]
    if present:
        return LocalBrainResult(
            task_id=request.task_id,
            status="FAILED_FINAL",
            reason_code="brain-authority-directive-rejected",
            detail="result attempted to set: " + ",".join(present),
            provider=result.provider,
        )

    required = request.result_schema.get("required", []) if request.result_schema else []
    missing = [key for key in required if key not in result.result]
    if result.ok and missing:
        return LocalBrainResult(
            task_id=request.task_id,
            status="FAILED_FINAL",
            reason_code="brain-result-schema-violation",
            detail="missing required fields: " + ",".join(missing),
            provider=result.provider,
        )

    return result
