"""Typed transport contracts.

Transport capability is deliberately separated from workflow authority. Nothing
in this module decides which role receives work, whether a transition is legal,
or whether work is approved. It only describes *mechanical* delivery to an
already-registered endpoint that workflow authority selected.

Two rules give that separation teeth:

1. Only the operations in ``TRANSPORT_OPERATIONS`` exist. There is deliberately
   no generic ``RUN_COMMAND(string)``; an adapter cannot be asked to execute an
   arbitrary program, and handoff prose therefore has nothing to aim at.
2. Every identifier that could steer a delivery -- endpoint, role, project,
   workflow, work item, artifact digest -- is supplied by the caller from
   persisted workflow state, never parsed out of handoff content.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


class TransportError(ValueError):
    """Raised when transport configuration or identity fails closed."""


# Namespace for deriving stable external correlation identities. Fixed so the
# same logical request always maps to the same external session id, which is
# what makes duplicate-submit detection and restart reconciliation possible.
ORBIT_TRANSPORT_NAMESPACE = uuid.UUID("6f9d2c1e-7a3b-4f5c-8d0e-1b2a3c4d5e6f")

TRANSPORT_OPERATIONS: Tuple[str, ...] = (
    "DELIVER_HANDOFF",
    "START_ASSIGNED_TASK",
    "QUERY_STATUS",
    "COLLECT_RESULT",
)

# Agent silence and process exit are NOT success. Every terminal classification
# an adapter may return is enumerated here, and COMPLETE is deliberately absent:
# only the workflow engine decides completion, from a validated handoff.
AGENT_RESULT_STATUSES: Tuple[str, ...] = (
    "WORKING",
    "READY_FOR_REVIEW",
    "BLOCKED",
    "NEEDS_DECISION",
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
)

TERMINAL_RESULT_STATUSES: Tuple[str, ...] = (
    "READY_FOR_REVIEW",
    "BLOCKED",
    "NEEDS_DECISION",
    "FAILED_FINAL",
)

TRANSPORT_STATES: Tuple[str, ...] = (
    "IDLE",
    "SUBMITTED",
    "COLLECTED",
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
    "STOPPED",
)


@dataclass(frozen=True)
class EndpointCapabilities:
    """The bounded authority an endpoint was registered with.

    An assignment can only ever narrow these. Nothing in a handoff or prompt can
    widen them, because the adapter reads capability exclusively from the
    registered endpoint.
    """

    tools: Tuple[str, ...] = ()
    permission_mode: str = "manual"
    add_dirs: Tuple[str, ...] = ()
    model: str = ""
    max_budget_usd: float = 0.0
    timeout_seconds: float = 600.0

    @staticmethod
    def from_config(value: Optional[Dict[str, Any]]) -> "EndpointCapabilities":
        value = value or {}
        permission_mode = str(value.get("permission_mode", "manual"))
        if permission_mode in {"bypassPermissions", "dontAsk"}:
            # Registering an endpoint that skips permission checks would let
            # transport quietly outrank Orbit's own authority model.
            raise TransportError(f"endpoint-permission-mode-not-allowed:{permission_mode}")
        try:
            budget = float(value.get("max_budget_usd", 0.0))
            timeout = float(value.get("timeout_seconds", 600.0))
        except (TypeError, ValueError) as exc:
            raise TransportError("endpoint-capability-not-numeric") from exc
        if budget < 0 or timeout <= 0:
            raise TransportError("endpoint-capability-out-of-range")
        return EndpointCapabilities(
            tools=tuple(str(t) for t in value.get("tools", ())),
            permission_mode=permission_mode,
            add_dirs=tuple(str(d) for d in value.get("add_dirs", ())),
            model=str(value.get("model", "")),
            max_budget_usd=budget,
            timeout_seconds=timeout,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tools": list(self.tools),
            "permission_mode": self.permission_mode,
            "add_dirs": list(self.add_dirs),
            "model": self.model,
            "max_budget_usd": self.max_budget_usd,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class AgentEndpoint:
    """A registered delivery target. Resolution is by exact id only."""

    endpoint_id: str
    role_id: str
    adapter_type: str
    project_id: str
    workflow_id: str
    work_item: str
    enabled: bool
    capabilities: EndpointCapabilities = field(default_factory=EndpointCapabilities)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "role_id": self.role_id,
            "adapter_type": self.adapter_type,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "work_item": self.work_item,
            "enabled": self.enabled,
            "capabilities": self.capabilities.to_dict(),
        }


@dataclass(frozen=True)
class TransportRequest:
    """One typed transport intent, fully identified before anything is started."""

    operation: str
    endpoint_id: str
    role_id: str
    project_id: str
    workflow_id: str
    work_item: str
    handoff_id: str
    artifact_digest: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation not in TRANSPORT_OPERATIONS:
            raise TransportError(f"transport-operation-not-allowlisted:{self.operation}")
        for name in ("endpoint_id", "role_id", "project_id", "workflow_id", "work_item"):
            if not str(getattr(self, name) or "").strip():
                raise TransportError(f"transport-request-missing:{name}")

    @property
    def request_id(self) -> str:
        """Stable identity for this exact intent.

        Deterministic so that a retry of the *same* intent is recognisable, and
        so that a different work item, digest or endpoint can never collide with
        an in-flight request.
        """
        payload = "|".join(
            [
                self.operation,
                self.project_id,
                self.workflow_id,
                self.work_item,
                self.endpoint_id,
                self.role_id,
                self.handoff_id,
                self.artifact_digest,
            ]
        ).encode("utf-8")
        return "treq-" + hashlib.sha256(payload).hexdigest()[:24]

    @property
    def correlation_id(self) -> str:
        """Deterministic external session identity (a valid UUID).

        Derived from request_id, so submitting the same request twice targets the
        same external session instead of starting a second one.
        """
        return str(uuid.uuid5(ORBIT_TRANSPORT_NAMESPACE, self.request_id))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "operation": self.operation,
            "endpoint_id": self.endpoint_id,
            "role_id": self.role_id,
            "project_id": self.project_id,
            "workflow_id": self.workflow_id,
            "work_item": self.work_item,
            "handoff_id": self.handoff_id,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True)
class AgentResult:
    """What an adapter observed. Never an approval, never a transition."""

    status: str
    reason_code: str = ""
    detail: str = ""
    correlation_id: str = ""
    result_path: Optional[str] = None
    artifact_digest: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in AGENT_RESULT_STATUSES:
            raise TransportError(f"agent-result-status-not-allowlisted:{self.status}")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RESULT_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "correlation_id": self.correlation_id,
            "result_path": self.result_path,
            "artifact_digest": self.artifact_digest,
            "evidence": self.evidence,
        }
