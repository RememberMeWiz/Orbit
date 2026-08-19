"""Chat transport contracts.

A deliberately narrow surface. The operations below are the *only* things Orbit
may ask a chat adapter to do, and none of them accepts coordinates, selectors,
key sequences, or an executable path. Handoff prose therefore has nothing to aim
at: it cannot name a window, a control, or a destination.

Nothing here performs GUI automation. These are the typed shapes an adapter must
satisfy; whether any adapter can satisfy them on a given host is a separate
question answered by ``diagnostics``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


class BridgeError(ValueError):
    """Raised when a bridge request or endpoint is malformed. Denials are results."""


CHAT_APPS: Tuple[str, ...] = ("CHATGPT_DESKTOP",)

# The complete operation surface. There is intentionally no CLICK, no
# TYPE_KEYS, no RUN_GUI_SCRIPT, and no CONTROL_WINDOW.
CHAT_OPERATIONS: Tuple[str, ...] = (
    "FOCUS_REGISTERED_CHAT",
    "ATTACH_ARTIFACT",
    "SEND_BOUNDED_MESSAGE",
    "WAIT_FOR_RESPONSE",
    "READ_LATEST_RESPONSE",
    "COLLECT_EXPECTED_ARTIFACT",
    "REPORT_TO_PM",
)

# Delivery lifecycle. AMBIGUOUS is a first-class state precisely because a crash
# between "send" and "receipt persisted" must never be resolved by guessing.
DELIVERY_STATES: Tuple[str, ...] = (
    "PENDING_SEND",
    "SENT_UNCONFIRMED",
    "DELIVERED",
    "FAILED",
    "AMBIGUOUS",
)


@dataclass(frozen=True)
class ChatEndpoint:
    """A registered conversation. Never inferred, only registered."""

    endpoint_id: str
    role_id: str
    app: str
    conversation_identity: str
    display_title: str
    project_scope: str
    workflow_scope: str
    enabled: bool = False
    verification_anchor: str = ""

    def __post_init__(self) -> None:
        for name in ("endpoint_id", "role_id", "app", "display_title", "project_scope", "workflow_scope"):
            if not str(getattr(self, name) or "").strip():
                raise BridgeError(f"endpoint-missing-field:{name}")
        if self.app not in CHAT_APPS:
            raise BridgeError(f"endpoint-app-not-supported:{self.app}")

    @property
    def identity_digest(self) -> str:
        payload = "|".join([self.app, self.conversation_identity, self.display_title,
                            self.project_scope, self.workflow_scope]).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:24]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "role_id": self.role_id,
            "app": self.app,
            "conversation_identity": self.conversation_identity,
            "display_title": self.display_title,
            "project_scope": self.project_scope,
            "workflow_scope": self.workflow_scope,
            "enabled": self.enabled,
            "verification_anchor": self.verification_anchor,
            "identity_digest": self.identity_digest,
        }


@dataclass(frozen=True)
class ChatTransportRequest:
    operation: str
    endpoint_id: str
    work_item: str
    request_id: str
    artifact_path: str = ""
    artifact_digest: str = ""
    message: str = ""
    options: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation not in CHAT_OPERATIONS:
            raise BridgeError(f"chat-operation-not-allowlisted:{self.operation}")
        for name in ("endpoint_id", "work_item", "request_id"):
            if not str(getattr(self, name) or "").strip():
                raise BridgeError(f"chat-request-missing:{name}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "endpoint_id": self.endpoint_id,
            "work_item": self.work_item,
            "request_id": self.request_id,
            "artifact_path": self.artifact_path,
            "artifact_digest": self.artifact_digest,
            "message_length": len(self.message),
        }


@dataclass(frozen=True)
class ChatTransportResult:
    ok: bool
    operation: str
    reason_code: str = ""
    detail: str = ""
    delivery_state: str = ""
    data: Optional[Dict[str, Any]] = None

    @staticmethod
    def deny(operation: str, reason_code: str, detail: str = "", delivery_state: str = "") -> "ChatTransportResult":
        # Denials carry an empty dict rather than None so callers can read
        # result.data uniformly without a null check on every branch.
        return ChatTransportResult(False, operation, reason_code, detail, delivery_state, {})

    @staticmethod
    def allow(operation: str, data: Dict[str, Any], delivery_state: str = "") -> "ChatTransportResult":
        return ChatTransportResult(True, operation, "ok", "", delivery_state, data)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "delivery_state": self.delivery_state,
            "data": self.data,
        }
