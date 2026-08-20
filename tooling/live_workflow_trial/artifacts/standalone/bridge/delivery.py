"""Durable send lifecycle.

The hazard this exists to remove: pressing Send is an *external* effect. Once
actuated, Orbit cannot know from its own memory whether the message left. If the
process dies between the click and the receipt, a naive ledger shows "no receipt"
and a restart happily sends again.

So intent is written to disk **before** actuation, not after:

    PENDING_SEND ── stage ──▶ STAGED_VERIFIED ── record ──▶ SEND_ACTUATED
                                                                │
                                                          (press Send)
                                                                │
                                                                ▼
                                                        SENT_UNCONFIRMED
                                                                │
                                                          (confirmed)
                                                                ▼
                                                            DELIVERED

A record found in `SEND_ACTUATED` at load time is exactly the uncertain window:
the click may or may not have landed. It is reconciled to `AMBIGUOUS`, and
`AMBIGUOUS` never auto-resends — a human decides. Anything still pre-actuation
is safe to retry, because nothing external happened yet.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from workflow.core.storage import atomic_write_json, utc_now_iso

DELIVERY_SCHEMA = "orbit.delivery-ledger/0.1-draft"

# Ordered, and the order is load-bearing: everything before SEND_ACTUATED is
# retryable because no external effect has occurred yet.
DELIVERY_STATES: Tuple[str, ...] = (
    "PENDING_SEND",
    "STAGED_VERIFIED",
    "SEND_ACTUATED",
    "SENT_UNCONFIRMED",
    "DELIVERED",
    "FAILED",
    "AMBIGUOUS",
)

RETRYABLE_STATES: Tuple[str, ...] = ("PENDING_SEND", "STAGED_VERIFIED", "FAILED")
TERMINAL_STATES: Tuple[str, ...] = ("DELIVERED", "AMBIGUOUS")


class DeliveryError(ValueError):
    """Raised on ledger misuse. Delivery refusals are results, not exceptions."""


def digest_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def digest_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class DeliveryLedger:
    """Durable per-request send state, bound to one work item."""

    def __init__(self, path: Path, *, work_item: str):
        self.path = Path(path)
        self.work_item = work_item

    # -- persistence -----------------------------------------------------

    def _blank(self) -> Dict[str, Any]:
        return {
            "schema_version": DELIVERY_SCHEMA,
            "work_item": self.work_item,
            "records": {},
            "state_revision": 0,
            "updated_at": utc_now_iso(),
        }

    def load(self) -> Dict[str, Any]:
        """Load and reconcile. Reconciliation happens on read, deliberately.

        A crash leaves no opportunity to run cleanup, so the uncertain state has
        to be resolved by whoever next opens the ledger.
        """
        if not self.path.exists():
            state = self._blank()
            self.save(state)
            return state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryError("delivery-ledger-malformed") from exc

        existing = state.get("work_item")
        if existing is not None and existing != self.work_item:
            raise DeliveryError("delivery-ledger-work-item-mismatch")
        state.setdefault("records", {})

        changed = False
        for record in state["records"].values():
            if record.get("state") == "SEND_ACTUATED":
                # We actuated and never got to record the outcome. Whether it
                # left is unknowable from here.
                record["state"] = "AMBIGUOUS"
                record["reason_code"] = "crash-after-actuation"
                record["reconciled_at"] = utc_now_iso()
                changed = True
        if changed:
            self.save(state)
        return state

    def save(self, state: Dict[str, Any]) -> None:
        state["state_revision"] = int(state.get("state_revision", 0)) + 1
        state["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, state)

    # -- records ---------------------------------------------------------

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.load()["records"].get(request_id)

    def _put(self, request_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
        state = self.load()
        state["records"][request_id] = record
        self.save(state)
        return record

    def begin(
        self,
        *,
        request_id: str,
        endpoint_id: str,
        artifact_digest: str = "",
        message_digest: str = "",
    ) -> Dict[str, Any]:
        """Open or resume a delivery. Refuses to reopen a settled one."""
        existing = self.get(request_id)
        if existing:
            state = existing.get("state")
            if state in TERMINAL_STATES:
                return existing
            if state == "SENT_UNCONFIRMED":
                return existing
            if state not in RETRYABLE_STATES:
                raise DeliveryError(f"delivery-not-resumable:{state}")
            # Retrying: the payload must still be the one we validated.
            existing["attempt"] = int(existing.get("attempt", 0)) + 1
            existing["state"] = "PENDING_SEND"
            existing["reason_code"] = "retry"
            existing["updated_at"] = utc_now_iso()
            return self._put(request_id, existing)

        return self._put(request_id, {
            "request_id": request_id,
            "work_item": self.work_item,
            "endpoint_id": endpoint_id,
            "artifact_digest": artifact_digest,
            "message_digest": message_digest,
            "state": "PENDING_SEND",
            "reason_code": "opened",
            "attempt": 1,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        })

    def mark_staged(self, request_id: str, *, artifact_digest: str, message_digest: str) -> Dict[str, Any]:
        record = self.get(request_id)
        if record is None:
            raise DeliveryError("delivery-record-missing")
        if record["state"] not in ("PENDING_SEND", "STAGED_VERIFIED"):
            raise DeliveryError(f"delivery-cannot-stage-from:{record['state']}")
        record["state"] = "STAGED_VERIFIED"
        record["artifact_digest"] = artifact_digest
        record["message_digest"] = message_digest
        record["reason_code"] = "staged-and-verified"
        record["updated_at"] = utc_now_iso()
        return self._put(request_id, record)

    def mark_actuating(self, request_id: str, *, artifact_digest: str, message_digest: str) -> Dict[str, Any]:
        """Write the intent to actuate BEFORE actuating.

        Also re-checks the payload: if the artifact or message changed between
        staging and this moment, the delivery is refused rather than sending
        something that was never validated.
        """
        record = self.get(request_id)
        if record is None:
            raise DeliveryError("delivery-record-missing")
        if record["state"] != "STAGED_VERIFIED":
            raise DeliveryError(f"delivery-cannot-actuate-from:{record['state']}")
        if record.get("artifact_digest", "") != artifact_digest:
            raise DeliveryError("delivery-artifact-changed-since-staging")
        if record.get("message_digest", "") != message_digest:
            raise DeliveryError("delivery-message-changed-since-staging")
        record["state"] = "SEND_ACTUATED"
        record["reason_code"] = "about-to-press-send"
        record["actuated_at"] = utc_now_iso()
        record["updated_at"] = utc_now_iso()
        return self._put(request_id, record)

    def mark_sent(self, request_id: str) -> Dict[str, Any]:
        record = self.get(request_id)
        if record is None:
            raise DeliveryError("delivery-record-missing")
        record["state"] = "SENT_UNCONFIRMED"
        record["reason_code"] = "send-returned"
        record["updated_at"] = utc_now_iso()
        return self._put(request_id, record)

    def mark_delivered(self, request_id: str, *, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        record = self.get(request_id)
        if record is None:
            raise DeliveryError("delivery-record-missing")
        record["state"] = "DELIVERED"
        record["reason_code"] = "confirmed"
        record["evidence"] = evidence or {}
        record["updated_at"] = utc_now_iso()
        return self._put(request_id, record)

    def mark_failed(self, request_id: str, *, reason_code: str) -> Dict[str, Any]:
        """Only usable pre-actuation. After actuation the truth is unknown."""
        record = self.get(request_id)
        if record is None:
            raise DeliveryError("delivery-record-missing")
        # Both post-actuation states must stay ambiguous. AMBIGUOUS appears here
        # as well as SEND_ACTUATED because load() reconciles the latter into the
        # former on read -- so an in-process failure reported right after
        # actuation already sees AMBIGUOUS. Matching only SEND_ACTUATED would
        # silently downgrade it to FAILED, which is retryable, and that is
        # precisely the double-send this ledger exists to prevent.
        if record["state"] in ("SEND_ACTUATED", "AMBIGUOUS"):
            record["state"] = "AMBIGUOUS"
            record["reason_code"] = f"actuated-then-failed:{reason_code}"
        else:
            record["state"] = "FAILED"
            record["reason_code"] = reason_code
        record["updated_at"] = utc_now_iso()
        return self._put(request_id, record)

    def mark_ambiguous(self, request_id: str, *, reason_code: str) -> Dict[str, Any]:
        record = self.get(request_id)
        if record is None:
            raise DeliveryError("delivery-record-missing")
        record["state"] = "AMBIGUOUS"
        record["reason_code"] = reason_code
        record["updated_at"] = utc_now_iso()
        return self._put(request_id, record)

    # -- decisions -------------------------------------------------------

    def may_send(self, request_id: str) -> Tuple[bool, str]:
        """Whether a send may be attempted, and why not when it may not."""
        record = self.get(request_id)
        if record is None:
            return True, "no-prior-attempt"
        state = record["state"]
        if state == "DELIVERED":
            return False, "already-delivered"
        if state == "AMBIGUOUS":
            # The whole point. A resend here could double-deliver.
            return False, "ambiguous-requires-human-disposition"
        if state == "SENT_UNCONFIRMED":
            return False, "awaiting-confirmation"
        if state == "SEND_ACTUATED":
            return False, "actuation-in-flight"
        return True, "retryable"

    def open_records(self) -> Dict[str, Dict[str, Any]]:
        return {
            rid: rec for rid, rec in self.load()["records"].items()
            if rec.get("state") not in TERMINAL_STATES
        }
