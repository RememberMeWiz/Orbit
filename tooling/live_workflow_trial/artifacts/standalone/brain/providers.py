"""Brain providers and the routing policy that keeps them optional.

The canonical invariant is that Orbit keeps working when every external service
disappears. That is implemented here: providers are tried in policy order, an
unavailable or quota-exhausted provider is skipped rather than fatal, and if
nothing can answer the router returns a typed BLOCKED result. No provider
failure is allowed to raise into workflow state.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence

from .contracts import BrainError, LocalBrainRequest, LocalBrainResult, validate_result


class BrainProvider(Protocol):
    name: str
    requires_network: bool

    def available(self) -> bool:
        """Cheap local check. Must never raise and never block on the network."""

    def reason(self, request: LocalBrainRequest) -> LocalBrainResult:
        """Produce a result. Should return typed failures rather than raise."""


class BrainRouter:
    """Selects a provider under policy, with guaranteed local fallback."""

    def __init__(self, providers: Sequence[BrainProvider], *, allow_network: bool = False):
        if not providers:
            raise BrainError("brain-router-no-providers")
        self.providers: List[BrainProvider] = list(providers)
        self.allow_network = allow_network

    def local_providers(self) -> List[BrainProvider]:
        return [p for p in self.providers if not getattr(p, "requires_network", False)]

    def eligible(self) -> List[BrainProvider]:
        """Providers policy currently permits, in preference order."""
        chosen = []
        for provider in self.providers:
            if getattr(provider, "requires_network", False) and not self.allow_network:
                # Network providers are accelerators. With network disallowed
                # they are simply not eligible; this is not an error condition.
                continue
            chosen.append(provider)
        return chosen

    def reason(self, request: LocalBrainRequest) -> LocalBrainResult:
        attempted: List[Dict[str, Any]] = []
        for provider in self.eligible():
            try:
                if not provider.available():
                    attempted.append({"provider": provider.name, "outcome": "unavailable"})
                    continue
                result = provider.reason(request)
            except Exception as exc:  # noqa: BLE001 - provider faults must not escape
                # A provider that throws is treated exactly like one that is
                # down. Quota exhaustion, a crashed subprocess and a bug all
                # degrade the same way: try the next one.
                attempted.append({"provider": provider.name, "outcome": f"error:{type(exc).__name__}"})
                continue

            validated = validate_result(request, result)
            attempted.append({"provider": provider.name, "outcome": validated.status})
            if validated.status in ("OK", "BLOCKED", "NEEDS_DECISION", "FAILED_FINAL"):
                evidence = dict(validated.evidence)
                evidence["provider_attempts"] = attempted
                return LocalBrainResult(
                    task_id=validated.task_id,
                    status=validated.status,
                    result=validated.result,
                    reason_code=validated.reason_code,
                    detail=validated.detail,
                    used_capabilities=validated.used_capabilities,
                    provider=validated.provider or provider.name,
                    evidence=evidence,
                )
            # FAILED_RETRYABLE falls through to the next provider.

        return LocalBrainResult(
            task_id=request.task_id,
            status="BLOCKED",
            reason_code="brain-no-provider-available",
            detail="no eligible brain provider could answer",
            evidence={"provider_attempts": attempted},
        )


class DeterministicBrain:
    """Fully local, offline, rule-based provider.

    This exists to validate orchestration mechanics end to end without any model
    weights, network, or credentials. It is deliberately transparent: given the
    same request it returns the same result, so scheduler and agent behaviour can
    be tested exactly.

    It is NOT a reasoning model and must never be presented as one.
    """

    name = "deterministic-local"
    requires_network = False
    is_reasoning_model = False

    def __init__(self, handlers: Optional[Dict[str, Any]] = None):
        self.handlers = dict(handlers or {})

    def available(self) -> bool:
        return True

    def reason(self, request: LocalBrainRequest) -> LocalBrainResult:
        handler = self.handlers.get(request.role)
        if handler is not None:
            return handler(request)
        return self._default(request)

    def _default(self, request: LocalBrainRequest) -> LocalBrainResult:
        directive = str(request.context.get("directive", "")).upper()
        if directive in ("BLOCKED", "NEEDS_DECISION"):
            # An input that explicitly encodes a blocker is preserved as one.
            # Blockers are never smoothed into success.
            return LocalBrainResult(
                task_id=request.task_id,
                status=directive,
                reason_code=f"directive-{directive.lower()}",
                detail=str(request.context.get("reason", "")),
                provider=self.name,
            )
        summary = f"{request.role} completed objective: {request.objective}"
        return LocalBrainResult(
            task_id=request.task_id,
            status="OK",
            result={"summary": summary, "role": request.role, "objective": request.objective},
            reason_code="deterministic-complete",
            used_capabilities=(),
            provider=self.name,
        )


class LocalModelBrain:
    """Seam for a real local reasoning model. Inactive until weights exist.

    Wired but deliberately fail-closed: with no model configured ``available()``
    is False, so the router skips it exactly like any other unavailable
    accelerator and Orbit keeps running on the deterministic path. Activating it
    is a configuration change, not a code change.
    """

    name = "local-model"
    requires_network = False
    is_reasoning_model = True

    def __init__(self, model_path: Optional[str] = None, runner=None):
        self.model_path = model_path
        self._runner = runner

    def available(self) -> bool:
        if self._runner is not None:
            return True
        if not self.model_path:
            return False
        from pathlib import Path

        return Path(self.model_path).exists()

    def reason(self, request: LocalBrainRequest) -> LocalBrainResult:
        if self._runner is None:
            return LocalBrainResult(
                task_id=request.task_id,
                status="FAILED_RETRYABLE",
                reason_code="local-model-not-configured",
                detail="no local model runtime or weights are configured on this host",
                provider=self.name,
            )
        return self._runner(request)
