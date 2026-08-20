"""PM-supervised chat bridge.

Endpoint registry, PM control envelope, teaching traces, and a feasibility
diagnostic. Transport adapters are deliberately absent: see diagnostics for
whether the installed app can be driven semantically at all.
"""
from .contracts import (
    CHAT_APPS,
    CHAT_OPERATIONS,
    DELIVERY_STATES,
    BridgeError,
    ChatEndpoint,
    ChatTransportRequest,
    ChatTransportResult,
)
from .delivery import DELIVERY_STATES, DeliveryError, DeliveryLedger, digest_text
from .diagnostics import FeasibilityReport, assess, probe_uia, run as run_diagnostic
from .orchestrator import ApprenticeLoop, LoopOutcome
from .pm_envelope import (
    DIRECTIVE_ACTIONS,
    DirectiveVerdict,
    PMBridgeState,
    PMDirective,
    PMRequest,
    parse_envelope,
    request_identity,
)
from .chatgpt import ChatGptAdapter, ResponseObservation
from .uia import UiaDriver, UiaResult
from .registry import ChatEndpointRegistry, KNOWN_ROLE_SLUGS, fold_title
from .teaching import TeachingTrace, TeachingTraceStore, condition_digest

__all__ = [
    "CHAT_APPS", "CHAT_OPERATIONS", "DELIVERY_STATES", "DIRECTIVE_ACTIONS",
    "KNOWN_ROLE_SLUGS", "BridgeError", "ChatEndpoint", "ChatEndpointRegistry",
    "ChatTransportRequest", "ChatTransportResult", "DirectiveVerdict",
    "DELIVERY_STATES", "DeliveryError", "DeliveryLedger", "digest_text",
    "ApprenticeLoop", "LoopOutcome",
    "FeasibilityReport", "PMBridgeState", "PMDirective", "PMRequest",
    "TeachingTrace", "TeachingTraceStore", "assess", "condition_digest",
    "fold_title", "parse_envelope", "probe_uia", "request_identity", "run_diagnostic",
    "ResponseObservation", "UiaDriver", "UiaResult",
]
