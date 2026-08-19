"""Adapter interface.

An adapter is mechanical only. It may start, observe and collect. It may not
choose a recipient, invent a transition, approve work, or reinterpret an agent's
own BLOCKED/NEEDS_DECISION signal as completion.
"""
from __future__ import annotations

from typing import Any, Dict, Protocol

from ..contracts import AgentEndpoint, AgentResult, TransportRequest


class LocalAdapter(Protocol):
    adapter_type: str

    def start(
        self,
        request: TransportRequest,
        endpoint: AgentEndpoint,
        *,
        correlation_id: str,
        assignment: Dict[str, Any],
    ) -> AgentResult:
        """Begin the assigned task. Must be safe to call at most once per request."""

    def query(
        self,
        request: TransportRequest,
        endpoint: AgentEndpoint,
        *,
        correlation_id: str,
    ) -> AgentResult:
        """Report bounded status from authoritative adapter evidence."""

    def collect(
        self,
        request: TransportRequest,
        endpoint: AgentEndpoint,
        *,
        correlation_id: str,
    ) -> AgentResult:
        """Collect exactly the expected one-file result, or fail closed."""
