from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

from .storage import atomic_write_json, utc_now_iso


class StateStore:
    def __init__(self, path: Path, manifest: Dict[str, Any]):
        self.path = path
        self.manifest = manifest

    def initial(self) -> Dict[str, Any]:
        stages = self.manifest["stages"]
        initial_stage = self.manifest.get("initial_stage", stages[0])
        initial_owner = self.manifest.get("initial_owner_role", initial_stage)
        return {
            "workflow_id": self.manifest["workflow_id"],
            "project_id": self.manifest["project_id"],
            "workflow_manifest_version": self.manifest.get("schema_version", "orbit.workflow-contracts/0.1-draft"),
            "work_item": self.manifest["work_item"],
            "current_owner_role": initial_owner,
            "current_stage": initial_stage,
            "work_state": "ASSIGNED",
            "delivery_state": "IDLE",
            "approval_state": "IDLE",
            "last_sequence": 0,
            "last_handoff_id": None,
            "last_artifact_digest": None,
            "pending_approval": None,
            "pending_delivery": None,
            "blocker_state": None,
            "accepted_handoff_ids": [],
            "accepted_handoff_digests": {},
            "approval_records": {},
            "state_revision": 0,
            "updated_at": utc_now_iso(),
            "schema_version": "orbit.workflow-state/0.1-draft",
        }

    def load(self) -> Dict[str, Any]:
        import json

        if not self.path.exists():
            state = self.initial()
            self.save(state)
            return state
        state = json.loads(self.path.read_text(encoding="utf-8"))
        for identity_key in ("project_id", "workflow_id", "work_item"):
            existing = state.get(identity_key)
            expected = self.manifest.get(identity_key)
            if existing is not None and existing != expected:
                raise ValueError(f"state-{identity_key}-mismatch")
        defaults = self.initial()
        changed = False
        for key, value in defaults.items():
            if key not in state:
                state[key] = copy.deepcopy(value)
                changed = True
        if changed:
            self.save(state)
        return state

    def save(self, state: Dict[str, Any]) -> None:
        state["state_revision"] = int(state.get("state_revision", 0)) + 1
        state["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, state)
