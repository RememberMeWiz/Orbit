"""Transport core: exact-once submission, restart reconciliation, STOP.

The core owns the decisions that must not vary per adapter:

* an endpoint is resolved from the registry, never from handoff content;
* a request is persisted as SUBMITTED before the external agent is started;
* a repeat of the same request reconciles instead of starting a second task;
* STOP prevents new starts and prevents a late result from driving advancement.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .adapters.base import LocalAdapter
from .contracts import AgentEndpoint, AgentResult, TransportError, TransportRequest
from .registry import EndpointRegistry
from .state import TransportStore


class TransportCore:
    def __init__(
        self,
        *,
        registry: EndpointRegistry,
        store: TransportStore,
        adapters: Dict[str, LocalAdapter],
        stop_path: Optional[Path] = None,
    ):
        self.registry = registry
        self.store = store
        self.adapters = dict(adapters)
        self.stop_path = stop_path

    # -- helpers ---------------------------------------------------------

    def is_stopped(self) -> bool:
        return bool(self.stop_path and self.stop_path.is_file())

    def _endpoint(self, request: TransportRequest) -> AgentEndpoint:
        return self.registry.resolve(
            request.endpoint_id,
            role_id=request.role_id,
            project_id=request.project_id,
            workflow_id=request.workflow_id,
            work_item=request.work_item,
        )

    def _adapter(self, endpoint: AgentEndpoint) -> LocalAdapter:
        adapter = self.adapters.get(endpoint.adapter_type)
        if adapter is None:
            raise TransportError(f"adapter-not-available:{endpoint.adapter_type}")
        return adapter

    def _receipt(
        self,
        request: TransportRequest,
        endpoint: Optional[AgentEndpoint],
        *,
        attempt: int,
        result: str,
        reason_code: str = "",
        agent_result: Optional[AgentResult] = None,
        correlation_id: str = "",
    ) -> Dict[str, Any]:
        payload = {
            "request_id": request.request_id,
            "operation": request.operation,
            "work_item": request.work_item,
            "project_id": request.project_id,
            "workflow_id": request.workflow_id,
            "handoff_id": request.handoff_id,
            "artifact_digest": request.artifact_digest,
            "source_role": request.role_id,
            "destination_role": endpoint.role_id if endpoint else request.role_id,
            "endpoint_id": request.endpoint_id,
            "adapter_type": endpoint.adapter_type if endpoint else "none",
            "attempt": attempt,
            "result": result,
            "reason_code": reason_code,
            "correlation_id": correlation_id or request.correlation_id,
        }
        if agent_result is not None:
            payload["agent_result"] = agent_result.to_dict()
        return self.store.append_receipt(payload)

    # -- typed operations ------------------------------------------------

    def submit(self, request: TransportRequest, assignment: Dict[str, Any]) -> Dict[str, Any]:
        """START_ASSIGNED_TASK / DELIVER_HANDOFF with exact-once semantics."""
        if request.operation not in ("START_ASSIGNED_TASK", "DELIVER_HANDOFF"):
            raise TransportError(f"transport-operation-not-submittable:{request.operation}")

        state = self.store.load()
        record = self.store.record_for(state, request)

        # STOP is checked before anything external happens. A stopped work item
        # never starts new work, and this is deliberately not an error: it is a
        # recorded, resumable decision.
        if self.is_stopped():
            receipt = self._receipt(request, None, attempt=int((record or {}).get("attempt", 0)), result="STOPPED", reason_code="stop-active-no-new-delivery")
            return {"status": "STOPPED", "reason_code": "stop-active-no-new-delivery", "receipt": receipt, "record": record}

        endpoint = self._endpoint(request)
        adapter = self._adapter(endpoint)

        if record is not None:
            existing_state = record.get("transport_state")
            if existing_state == "SUBMITTED":
                # Already in flight. Reconcile against the external session
                # rather than starting a second task for the same request.
                reconciled = adapter.query(request, endpoint, correlation_id=record["correlation_id"])
                receipt = self._receipt(
                    request, endpoint,
                    attempt=int(record.get("attempt", 1)),
                    result="ALREADY_SUBMITTED",
                    reason_code="duplicate-submit-reconciled",
                    agent_result=reconciled,
                    correlation_id=record["correlation_id"],
                )
                return {"status": "ALREADY_SUBMITTED", "reason_code": "duplicate-submit-reconciled", "agent_result": reconciled, "receipt": receipt, "record": record}
            if existing_state == "COLLECTED":
                receipt = self._receipt(
                    request, endpoint,
                    attempt=int(record.get("attempt", 1)),
                    result="ALREADY_COLLECTED",
                    reason_code="request-already-completed",
                    correlation_id=record["correlation_id"],
                )
                return {"status": "ALREADY_COLLECTED", "reason_code": "request-already-completed", "receipt": receipt, "record": record}

        correlation_id = request.correlation_id

        # Persist intent BEFORE starting anything external. If the process dies
        # between here and the adapter call, restart sees SUBMITTED and
        # reconciles; it can never conclude "nothing was started".
        record = self.store.mark_submitted(state, request, correlation_id=correlation_id)

        agent_result = adapter.start(request, endpoint, correlation_id=correlation_id, assignment=assignment)

        transport_state = "SUBMITTED"
        if agent_result.status == "FAILED_FINAL":
            transport_state = "FAILED_FINAL"
        elif agent_result.status == "FAILED_RETRYABLE":
            transport_state = "FAILED_RETRYABLE"
        if transport_state != "SUBMITTED":
            state = self.store.load()
            record = self.store.mark_result(state, request, agent_result.to_dict(), transport_state=transport_state)

        receipt = self._receipt(
            request, endpoint,
            attempt=int(record.get("attempt", 1)),
            result=transport_state,
            reason_code=agent_result.reason_code,
            agent_result=agent_result,
            correlation_id=correlation_id,
        )
        return {"status": transport_state, "reason_code": agent_result.reason_code, "agent_result": agent_result, "receipt": receipt, "record": record}

    def query(self, request: TransportRequest) -> Dict[str, Any]:
        """QUERY_STATUS against authoritative adapter evidence."""
        state = self.store.load()
        record = self.store.record_for(state, request)
        if record is None:
            raise TransportError("transport-record-missing")
        endpoint = self._endpoint(request)
        adapter = self._adapter(endpoint)
        agent_result = adapter.query(request, endpoint, correlation_id=record["correlation_id"])
        receipt = self._receipt(
            request, endpoint,
            attempt=int(record.get("attempt", 1)),
            result="QUERIED",
            reason_code=agent_result.reason_code,
            agent_result=agent_result,
            correlation_id=record["correlation_id"],
        )
        return {"status": agent_result.status, "agent_result": agent_result, "receipt": receipt, "record": record}

    def collect(self, request: TransportRequest) -> Dict[str, Any]:
        """COLLECT_RESULT, exactly once, and never twice."""
        state = self.store.load()
        record = self.store.record_for(state, request)
        if record is None:
            raise TransportError("transport-record-missing")

        if record.get("transport_state") == "COLLECTED":
            # Collection is idempotent: a retry returns the stored result rather
            # than re-reading and risking a second workflow advancement.
            receipt = self._receipt(
                request, None,
                attempt=int(record.get("attempt", 1)),
                result="ALREADY_COLLECTED",
                reason_code="result-already-collected",
                correlation_id=record["correlation_id"],
            )
            return {"status": "ALREADY_COLLECTED", "reason_code": "result-already-collected", "result": record.get("result"), "receipt": receipt, "record": record}

        endpoint = self._endpoint(request)
        adapter = self._adapter(endpoint)
        agent_result = adapter.collect(request, endpoint, correlation_id=record["correlation_id"])

        # A result that lands while STOP is active is preserved for explicit
        # disposition. It is never auto-routed into workflow advancement.
        if self.is_stopped():
            record = self.store.mark_result(state, request, agent_result.to_dict(), transport_state="STOPPED", late=True)
            receipt = self._receipt(
                request, endpoint,
                attempt=int(record.get("attempt", 1)),
                result="STOPPED_LATE_RESULT",
                reason_code="stop-active-result-held",
                agent_result=agent_result,
                correlation_id=record["correlation_id"],
            )
            return {"status": "STOPPED_LATE_RESULT", "reason_code": "stop-active-result-held", "agent_result": agent_result, "receipt": receipt, "record": record}

        if agent_result.status in ("FAILED_FINAL", "FAILED_RETRYABLE"):
            record = self.store.mark_result(state, request, agent_result.to_dict(), transport_state=agent_result.status)
            receipt = self._receipt(
                request, endpoint,
                attempt=int(record.get("attempt", 1)),
                result=agent_result.status,
                reason_code=agent_result.reason_code,
                agent_result=agent_result,
                correlation_id=record["correlation_id"],
            )
            return {"status": agent_result.status, "reason_code": agent_result.reason_code, "agent_result": agent_result, "receipt": receipt, "record": record}

        if not agent_result.is_terminal:
            receipt = self._receipt(
                request, endpoint,
                attempt=int(record.get("attempt", 1)),
                result="WORKING",
                reason_code=agent_result.reason_code,
                agent_result=agent_result,
                correlation_id=record["correlation_id"],
            )
            return {"status": "WORKING", "agent_result": agent_result, "receipt": receipt, "record": record}

        record = self.store.mark_result(state, request, agent_result.to_dict(), transport_state="COLLECTED")
        receipt = self._receipt(
            request, endpoint,
            attempt=int(record.get("attempt", 1)),
            result="COLLECTED",
            reason_code=agent_result.reason_code,
            agent_result=agent_result,
            correlation_id=record["correlation_id"],
        )
        return {"status": "COLLECTED", "agent_result": agent_result, "receipt": receipt, "record": record}
