"""Chat endpoint registry: explicit registration, fail-closed resolution.

Two chats named "Orbit Windows Worker" and "Orbit Windows Worker (old)" must
never silently resolve to one of them. Resolution therefore takes the *observed*
candidates from whatever discovery mechanism the host provides and refuses to
proceed unless exactly one matches. Ambiguity is an error, never a best guess.

Handoff prose cannot reach any of this: an endpoint must already be registered,
and it is selected by `endpoint_id` supplied from governed workflow state.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from workflow.core.storage import atomic_write_json, utc_now_iso

from .contracts import BridgeError, ChatEndpoint

# Roles the apprenticeship loop expects to address. Registration is separate:
# being listed here does not make an endpoint usable.
KNOWN_ROLE_SLUGS = (
    "orbit-pm",
    "architecture-tl",
    "windows-worker",
    "android-worker",
    "memory-worker",
    "qa-safety",
    "product-research",
)


def fold_title(value: str) -> str:
    """Comparison form for a chat title.

    Case, spacing and punctuation are folded because a human renaming a chat
    from "Orbit PM" to "orbit-pm" should be recognised as *possibly the same
    chat* and therefore ambiguous, not as a clean miss.
    """
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


class ChatEndpointRegistry:
    def __init__(self, endpoints: Iterable[ChatEndpoint] = ()):
        self._by_id: Dict[str, ChatEndpoint] = {}
        folded: Dict[str, str] = {}
        for endpoint in endpoints:
            self._add(endpoint, folded)

    def _add(self, endpoint: ChatEndpoint, folded: Dict[str, str]) -> None:
        if endpoint.endpoint_id in self._by_id:
            raise BridgeError(f"endpoint-duplicate-id:{endpoint.endpoint_id}")
        key = (endpoint.app, fold_title(endpoint.display_title), endpoint.project_scope)
        marker = "|".join(key)
        if marker in folded:
            raise BridgeError(f"endpoint-ambiguous-title:{folded[marker]}~{endpoint.endpoint_id}")
        folded[marker] = endpoint.endpoint_id
        self._by_id[endpoint.endpoint_id] = endpoint

    # -- persistence -----------------------------------------------------

    @classmethod
    def from_orbit_config(cls, path: Optional[Path] = None) -> "ChatEndpointRegistry":
        """Load the committed Orbit endpoint configuration.

        Kept as data rather than code so adding a role chat is a reviewable
        config change, and so the enabled flag is visible at a glance:
        registration is not permission.
        """
        target = Path(path) if path else Path(__file__).with_name("orbit_endpoints.json")
        return cls.load(target)

    @classmethod
    def load(cls, path: Path) -> "ChatEndpointRegistry":
        if not Path(path).exists():
            return cls(())
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("endpoint-registry-malformed") from exc
        entries = raw.get("endpoints", {})
        if not isinstance(entries, dict):
            raise BridgeError("endpoint-registry-not-object")
        endpoints = []
        for endpoint_id, entry in entries.items():
            if not isinstance(entry, dict):
                raise BridgeError(f"endpoint-entry-not-object:{endpoint_id}")
            endpoints.append(ChatEndpoint(
                endpoint_id=endpoint_id,
                role_id=str(entry.get("role_id", "")),
                app=str(entry.get("app", "")),
                conversation_identity=str(entry.get("conversation_identity", "")),
                display_title=str(entry.get("display_title", "")),
                project_scope=str(entry.get("project_scope", "")),
                workflow_scope=str(entry.get("workflow_scope", "")),
                enabled=bool(entry.get("enabled", False)),
                verification_anchor=str(entry.get("verification_anchor", "")),
            ))
        return cls(endpoints)

    def save(self, path: Path) -> None:
        """Persist identity metadata only. No tokens, no message content."""
        payload = {
            "schema_version": "orbit.chat-endpoint-registry/0.1-draft",
            "updated_at": utc_now_iso(),
            "endpoints": {e.endpoint_id: e.to_dict() for e in self._by_id.values()},
        }
        atomic_write_json(Path(path), payload)

    # -- access ----------------------------------------------------------

    def ids(self) -> List[str]:
        return sorted(self._by_id)

    def get(self, endpoint_id: str) -> Optional[ChatEndpoint]:
        return self._by_id.get(endpoint_id)

    def resolve(
        self,
        endpoint_id: str,
        *,
        project_scope: str,
        workflow_scope: str,
        observed_titles: Optional[Sequence[str]] = None,
    ) -> ChatEndpoint:
        """Return the one endpoint that matches, or fail closed.

        ``observed_titles`` is what the host currently reports as open/visible
        conversations. When supplied it is checked for ambiguity: if more than
        one observed chat folds to the registered title, resolution refuses
        rather than picking one. When the endpoint's title is not observed at
        all, that is a miss -- a renamed or closed chat must not be sent to.
        """
        endpoint = self._by_id.get(endpoint_id)
        if endpoint is None:
            raise BridgeError("endpoint-not-registered")
        if not endpoint.enabled:
            raise BridgeError("endpoint-disabled")
        if endpoint.project_scope != project_scope:
            raise BridgeError("endpoint-project-scope-mismatch")
        if endpoint.workflow_scope != workflow_scope:
            raise BridgeError("endpoint-workflow-scope-mismatch")

        if observed_titles is not None:
            wanted = fold_title(endpoint.display_title)
            matches = [t for t in observed_titles if fold_title(t) == wanted]
            if not matches:
                raise BridgeError("endpoint-not-observed")
            if len(matches) > 1:
                raise BridgeError("endpoint-ambiguous-observed")
        return endpoint

    def register(self, endpoint: ChatEndpoint) -> ChatEndpoint:
        folded = {
            "|".join((e.app, fold_title(e.display_title), e.project_scope)): e.endpoint_id
            for e in self._by_id.values()
        }
        self._add(endpoint, folded)
        return endpoint
