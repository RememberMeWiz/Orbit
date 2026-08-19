from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable

from .storage import is_within


class RuntimeConfigurationError(ValueError):
    """Raised when trusted runtime configuration fails closed."""


@dataclass(frozen=True)
class RuntimePaths:
    artifacts_root: Path
    approved_trial_root: Path
    workspace: Path
    inbox: Path
    stop: Path
    state: Path
    receipts: Path


def _relative_config_path(value: Any, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise RuntimeConfigurationError(f"missing-{field}")
    win = PureWindowsPath(text)
    native = Path(text)
    if win.is_absolute() or win.drive or native.is_absolute():
        raise RuntimeConfigurationError(f"{field}-absolute-path-not-allowed")
    if ".." in win.parts or ".." in native.parts:
        raise RuntimeConfigurationError(f"{field}-parent-traversal-not-allowed")
    return native


def _require_keys(manifest: Dict[str, Any], keys: Iterable[str]) -> None:
    for key in keys:
        if not str(manifest.get(key, "")).strip():
            raise RuntimeConfigurationError(f"missing-manifest-field:{key}")


def validate_manifest_authority(manifest: Dict[str, Any]) -> None:
    _require_keys(manifest, ["project_id", "workflow_id", "work_item"])
    stages = manifest.get("stages")
    roles = manifest.get("roles")
    transitions = manifest.get("valid_transitions")
    destinations = manifest.get("destinations")
    registry = manifest.get("role_destination_registry")
    if not isinstance(stages, list) or not stages:
        raise RuntimeConfigurationError("invalid-stages")
    if not isinstance(roles, list) or not roles:
        raise RuntimeConfigurationError("invalid-roles")
    if not isinstance(transitions, dict):
        raise RuntimeConfigurationError("invalid-valid-transitions")
    if not isinstance(destinations, dict) or not destinations:
        raise RuntimeConfigurationError("invalid-destinations")
    if not isinstance(registry, dict) or not registry:
        raise RuntimeConfigurationError("invalid-role-destination-registry")

    initial_stage = str(manifest.get("initial_stage", stages[0]))
    initial_owner = str(manifest.get("initial_owner_role", initial_stage))
    if initial_stage not in stages:
        raise RuntimeConfigurationError("initial-stage-not-in-stages")
    if initial_owner not in roles:
        raise RuntimeConfigurationError("initial-owner-not-in-roles")

    if manifest.get("allowed_executor_operations") != ["PLACE_PACKET"]:
        raise RuntimeConfigurationError("executor-catalog-not-exactly-place-packet")

    for role, endpoint in destinations.items():
        entry = registry.get(role)
        if not isinstance(entry, dict):
            raise RuntimeConfigurationError(f"destination-not-registered:{role}")
        if entry.get("role_id") != role or entry.get("destination_id") != role:
            raise RuntimeConfigurationError(f"destination-registry-identity-mismatch:{role}")
        if entry.get("adapter_type") != "PLACE_PACKET":
            raise RuntimeConfigurationError(f"destination-adapter-not-place-packet:{role}")
        if str(entry.get("endpoint_ref", "")) != str(endpoint):
            raise RuntimeConfigurationError(f"destination-registry-config-mismatch:{role}")


def resolve_runtime_paths(root: Path, manifest: Dict[str, Any]) -> RuntimePaths:
    validate_manifest_authority(manifest)
    artifacts_root = (root / "artifacts").resolve()

    # Backward-compatible fixture resolution is derived from the configured inbox,
    # not from a literal sample_workspace path. Real live manifests set workspace
    # and approved_trial_root explicitly.
    inbox_rel = _relative_config_path(manifest.get("inbox"), "inbox")
    workspace_value = manifest.get("workspace")
    if workspace_value is None:
        workspace_rel = inbox_rel.parent
    else:
        workspace_rel = _relative_config_path(workspace_value, "workspace")

    approved_value = manifest.get("approved_trial_root")
    if approved_value is None:
        approved_rel = workspace_rel.parent if workspace_rel.parent != Path("") else Path(".")
    else:
        approved_rel = _relative_config_path(approved_value, "approved-trial-root")

    approved_trial_root = (artifacts_root / approved_rel).resolve()
    workspace = (artifacts_root / workspace_rel).resolve()
    inbox = (artifacts_root / inbox_rel).resolve()

    if not is_within(approved_trial_root, artifacts_root):
        raise RuntimeConfigurationError("approved-trial-root-outside-artifacts-root")
    if not is_within(workspace, approved_trial_root):
        raise RuntimeConfigurationError("workspace-outside-approved-trial-root")
    if not is_within(inbox, workspace):
        raise RuntimeConfigurationError("inbox-outside-configured-workspace")

    for role, value in manifest["destinations"].items():
        rel = _relative_config_path(value, f"destination-{role}")
        resolved = (artifacts_root / rel).resolve()
        if not is_within(resolved, approved_trial_root):
            raise RuntimeConfigurationError(f"destination-outside-approved-trial-root:{role}")

    return RuntimePaths(
        artifacts_root=artifacts_root,
        approved_trial_root=approved_trial_root,
        workspace=workspace,
        inbox=inbox,
        stop=workspace / "STOP",
        state=workspace / "state.json",
        receipts=workspace / "receipts" / "receipts.jsonl",
    )


def assert_expected_identity(
    manifest: Dict[str, Any],
    *,
    project_id: str | None = None,
    workflow_id: str | None = None,
    work_item: str | None = None,
) -> None:
    expected = {
        "project_id": project_id,
        "workflow_id": workflow_id,
        "work_item": work_item,
    }
    for field, value in expected.items():
        if value is not None and str(manifest.get(field)) != str(value):
            raise RuntimeConfigurationError(f"manifest-{field}-mismatch")
