"""Endpoint registry. Resolution is deny-by-default and exact-match only.

This mirrors the role/destination registry the PLACE_PACKET executor already
uses: configuration decides what exists, and a lookup either finds exactly one
enabled, in-scope entry or fails closed with a reason code.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from .contracts import AgentEndpoint, EndpointCapabilities, TransportError


def _normalize_label(value: str) -> str:
    """Fold a label to its comparison form.

    Endpoint ids are matched exactly, but ambiguity is detected on this folded
    form so that ``claude-worker``, ``Claude_Worker`` and ``claudeworker`` cannot
    coexist and turn a delivery into a coin flip.
    """
    return "".join(ch for ch in value.lower() if ch.isalnum())


class EndpointRegistry:
    def __init__(self, endpoints: Iterable[AgentEndpoint]):
        self._by_id: Dict[str, AgentEndpoint] = {}
        collisions: Dict[str, str] = {}
        for endpoint in endpoints:
            endpoint_id = endpoint.endpoint_id
            if endpoint_id in self._by_id:
                raise TransportError(f"endpoint-duplicate-id:{endpoint_id}")
            folded = _normalize_label(endpoint_id)
            if not folded:
                raise TransportError(f"endpoint-id-empty:{endpoint_id}")
            if folded in collisions:
                raise TransportError(
                    f"endpoint-ambiguous-label:{collisions[folded]}~{endpoint_id}"
                )
            collisions[folded] = endpoint_id
            self._by_id[endpoint_id] = endpoint

    @classmethod
    def from_config(cls, config: Any) -> "EndpointRegistry":
        """Build from the trusted transport configuration document."""
        if not isinstance(config, dict):
            raise TransportError("transport-registry-not-object")
        raw = config.get("agent_endpoint_registry")
        if not isinstance(raw, dict) or not raw:
            raise TransportError("transport-registry-missing-or-empty")

        endpoints = []
        for endpoint_id, entry in raw.items():
            if not isinstance(entry, dict):
                raise TransportError(f"endpoint-entry-not-object:{endpoint_id}")
            declared = str(entry.get("endpoint_id", endpoint_id))
            if declared != endpoint_id:
                raise TransportError(f"endpoint-identity-mismatch:{endpoint_id}")
            for required in ("role_id", "adapter_type", "project_id", "workflow_id", "work_item"):
                if not str(entry.get(required, "")).strip():
                    raise TransportError(f"endpoint-missing-field:{endpoint_id}.{required}")
            endpoints.append(
                AgentEndpoint(
                    endpoint_id=endpoint_id,
                    role_id=str(entry["role_id"]),
                    adapter_type=str(entry["adapter_type"]),
                    project_id=str(entry["project_id"]),
                    workflow_id=str(entry["workflow_id"]),
                    work_item=str(entry["work_item"]),
                    enabled=bool(entry.get("enabled", False)),
                    capabilities=EndpointCapabilities.from_config(entry.get("capabilities")),
                )
            )
        return cls(endpoints)

    def ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._by_id))

    def resolve(
        self,
        endpoint_id: str,
        *,
        role_id: str,
        project_id: str,
        workflow_id: str,
        work_item: str,
    ) -> AgentEndpoint:
        """Return the one endpoint that matches, or fail closed.

        Scope is checked as well as existence: an endpoint registered for another
        project, workflow, work item or role is a miss, not a fallback. This is
        what stops a valid-looking assignment from being delivered into a
        neighbouring project's session.
        """
        endpoint = self._by_id.get(endpoint_id)
        if endpoint is None:
            raise TransportError("endpoint-not-registered")
        if not endpoint.enabled:
            raise TransportError("endpoint-disabled")
        if endpoint.adapter_type not in SUPPORTED_ADAPTER_TYPES:
            raise TransportError(f"endpoint-adapter-not-supported:{endpoint.adapter_type}")
        if endpoint.role_id != role_id:
            raise TransportError("endpoint-role-mismatch")
        if endpoint.project_id != project_id:
            raise TransportError("endpoint-project-mismatch")
        if endpoint.workflow_id != workflow_id:
            raise TransportError("endpoint-workflow-mismatch")
        if endpoint.work_item != work_item:
            raise TransportError("endpoint-work-item-mismatch")
        return endpoint


SUPPORTED_ADAPTER_TYPES = ("CLAUDE_CODE_LOCAL",)
