from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable

from .runtime import RuntimeConfigurationError, RuntimePaths, assert_expected_identity, resolve_runtime_paths, validate_manifest_authority
from .state import StateStore
from .storage import atomic_write_json, is_within


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
BOOTSTRAP_MANIFEST_NAME = "manifest.json"


class BootstrapError(RuntimeConfigurationError):
    """Raised when a trusted bootstrap configuration fails closed."""


def _require_explicit(manifest: Dict[str, Any], fields: Iterable[str]) -> None:
    for field in fields:
        value = manifest.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise BootstrapError(f"missing-bootstrap-field:{field}")


def _require_identifier(manifest: Dict[str, Any], field: str) -> None:
    value = str(manifest.get(field, "")).strip()
    if not IDENTIFIER_RE.fullmatch(value):
        raise BootstrapError(f"malformed-{field}")


def _relative_path(value: Any, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise BootstrapError(f"missing-bootstrap-field:{field}")
    win = PureWindowsPath(text)
    native = Path(text)
    if win.is_absolute() or win.drive or native.is_absolute():
        raise BootstrapError(f"{field}-absolute-path-not-allowed")
    if ".." in win.parts or ".." in native.parts:
        raise BootstrapError(f"{field}-parent-traversal-not-allowed")
    return native


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _existing_reparse_components(base: Path, candidate: Path) -> list[Path]:
    """Return existing symlink/junction components on the lexical candidate path."""
    try:
        relative = candidate.relative_to(base)
    except ValueError:
        return [candidate]
    found: list[Path] = []
    current = base
    if current.exists() and _is_reparse(current):
        found.append(current)
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_reparse(current):
            found.append(current)
    return found


def _canonical_manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_digest(manifest: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_manifest_bytes(manifest)).hexdigest()


def _load_json_file(path: Path, reason: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(reason) from exc


def validate_bootstrap_configuration(root: Path, manifest: Dict[str, Any]) -> RuntimePaths:
    if not isinstance(manifest, dict):
        raise BootstrapError("bootstrap-config-not-object")

    _require_explicit(
        manifest,
        [
            "project_id",
            "workflow_id",
            "work_item",
            "work_item_title",
            "approved_trial_root",
            "workspace",
            "inbox",
            "initial_stage",
            "initial_owner_role",
            "stages",
            "roles",
            "role_destination_registry",
            "destinations",
            "valid_transitions",
            "approval_required_transitions",
            "allowed_executor_operations",
        ],
    )
    for field in ("project_id", "workflow_id", "work_item"):
        _require_identifier(manifest, field)
    if not str(manifest.get("work_item_title", "")).strip():
        raise BootstrapError("missing-bootstrap-field:work_item_title")

    try:
        validate_manifest_authority(manifest)
    except RuntimeConfigurationError as exc:
        raise BootstrapError(str(exc)) from exc

    stages = manifest.get("stages")
    roles = manifest.get("roles")
    if len(stages) != len(set(stages)) or any(not isinstance(x, str) or not x.strip() for x in stages):
        raise BootstrapError("invalid-or-duplicate-stages")
    if len(roles) != len(set(roles)) or any(not isinstance(x, str) or not x.strip() for x in roles):
        raise BootstrapError("invalid-or-duplicate-roles")

    transitions = manifest.get("valid_transitions")
    for sender, recipient in transitions.items():
        if sender not in stages or recipient not in stages:
            raise BootstrapError("transition-references-unknown-stage")

    approvals = manifest.get("approval_required_transitions")
    if not isinstance(approvals, list):
        raise BootstrapError("invalid-approval-required-transitions")
    for transition in approvals:
        if not isinstance(transition, str) or "->" not in transition:
            raise BootstrapError("invalid-approval-transition")
        sender, recipient = transition.split("->", 1)
        if transitions.get(sender) != recipient:
            raise BootstrapError("approval-transition-not-valid-transition")

    destinations = manifest.get("destinations", {})
    registry = manifest.get("role_destination_registry", {})
    if set(destinations) != set(registry):
        raise BootstrapError("destination-registry-keyset-mismatch")
    for role in roles:
        if role not in destinations:
            raise BootstrapError(f"missing-destination-for-role:{role}")

    endpoint_values: list[str] = []
    for key, value in destinations.items():
        rel = _relative_path(value, f"destination-{key}")
        endpoint_values.append(str(PureWindowsPath(str(rel))).lower())
        entry = registry.get(key)
        if not isinstance(entry, dict):
            raise BootstrapError(f"destination-not-registered:{key}")
        if entry.get("role_id") != key or entry.get("destination_id") != key:
            raise BootstrapError(f"destination-registry-identity-mismatch:{key}")
        if str(entry.get("endpoint_ref", "")) != str(value):
            raise BootstrapError(f"destination-registry-config-mismatch:{key}")
        if entry.get("adapter_type") != "PLACE_PACKET":
            raise BootstrapError(f"destination-adapter-not-place-packet:{key}")
    if len(endpoint_values) != len(set(endpoint_values)):
        raise BootstrapError("duplicate-destination-endpoint")

    try:
        paths = resolve_runtime_paths(root, manifest)
    except RuntimeConfigurationError as exc:
        raise BootstrapError(str(exc)) from exc

    artifacts_root = paths.artifacts_root
    approved_unresolved = artifacts_root / _relative_path(manifest["approved_trial_root"], "approved-trial-root")
    workspace_unresolved = artifacts_root / _relative_path(manifest["workspace"], "workspace")
    inbox_unresolved = artifacts_root / _relative_path(manifest["inbox"], "inbox")

    # Validate every write target before creating anything. Existing reparse
    # components are rejected because the accepted PLACE_PACKET adapter also
    # denies them; bootstrap must not create a route the runtime cannot safely use.
    configured_targets = {
        "approved-trial-root": approved_unresolved,
        "workspace": workspace_unresolved,
        "inbox": inbox_unresolved,
    }
    for key, value in destinations.items():
        configured_targets[f"destination-{key}"] = artifacts_root / _relative_path(value, f"destination-{key}")

    for label, unresolved in configured_targets.items():
        reparses = _existing_reparse_components(artifacts_root, unresolved)
        if reparses:
            # For the requested workspace/junction escape case, Path.resolve()
            # proves whether the link would leave the approved root. We reject
            # all existing reparse components to match runtime placement policy.
            resolved = unresolved.resolve()
            escaped = not is_within(resolved, paths.approved_trial_root)
            suffix = "-escape" if escaped else ""
            raise BootstrapError(f"{label}-reparse-point-not-allowed{suffix}")

    return paths


def _validate_existing_instance(paths: RuntimePaths, manifest: Dict[str, Any]) -> tuple[Path, bool]:
    manifest_path = paths.workspace / BOOTSTRAP_MANIFEST_NAME
    state_path = paths.state
    receipts_path = paths.receipts
    stop_path = paths.stop

    for label, path in (
        ("workspace", paths.workspace),
        ("inbox", paths.inbox),
        ("receipts-directory", receipts_path.parent),
    ):
        if path.exists() and not path.is_dir():
            raise BootstrapError(f"incompatible-existing-{label}")

    for label, path in (
        ("manifest", manifest_path),
        ("state", state_path),
        ("receipts", receipts_path),
    ):
        if path.exists() and (not path.is_file() or _is_reparse(path)):
            raise BootstrapError(f"incompatible-existing-{label}")

    if stop_path.exists() and (not stop_path.is_file() or _is_reparse(stop_path)):
        raise BootstrapError("incompatible-existing-stop-control")

    for key, value in manifest["destinations"].items():
        destination = (paths.artifacts_root / Path(str(value))).resolve()
        if destination.exists() and not destination.is_dir():
            raise BootstrapError(f"incompatible-existing-destination:{key}")

    manifest_exists = manifest_path.exists()
    state_exists = state_path.exists()
    receipts_exists = receipts_path.exists()

    if manifest_exists:
        existing_manifest = _load_json_file(manifest_path, "existing-manifest-malformed")
        if existing_manifest != manifest:
            raise BootstrapError("existing-manifest-incompatible")
    elif state_exists or receipts_exists:
        raise BootstrapError("existing-workflow-material-without-manifest")
    elif paths.workspace.exists():
        # A pre-existing STOP is explicitly preserved. Any other file without
        # a manifest is ambiguous and must not be adopted as a workflow.
        files = [p for p in paths.workspace.rglob("*") if p.is_file() or p.is_symlink()]
        unexpected = [p for p in files if p != stop_path]
        if unexpected:
            raise BootstrapError("ambiguous-preexisting-workspace")

    if state_exists:
        state = _load_json_file(state_path, "existing-state-malformed")
        if not isinstance(state, dict):
            raise BootstrapError("existing-state-not-object")
        for field in ("project_id", "workflow_id", "work_item"):
            if state.get(field) != manifest.get(field):
                raise BootstrapError(f"state-{field}-mismatch")

    return manifest_path, manifest_exists and state_exists


def bootstrap_workspace(
    root: Path,
    manifest: Dict[str, Any],
    *,
    project_id: str,
    workflow_id: str,
    work_item: str,
) -> Dict[str, Any]:
    """Initialize one bounded workflow workspace from trusted configuration.

    Validation and collision checks complete before filesystem creation. State is
    written last, so a failed initialization cannot leave accepted workflow state.
    Re-running a compatible initialized instance is read-only and deterministic.
    """
    root = Path(root)
    try:
        assert_expected_identity(
            manifest,
            project_id=project_id,
            workflow_id=workflow_id,
            work_item=work_item,
        )
    except RuntimeConfigurationError as exc:
        raise BootstrapError(str(exc)) from exc
    paths = validate_bootstrap_configuration(root, manifest)
    manifest_path, already_initialized = _validate_existing_instance(paths, manifest)

    if already_initialized:
        state = _load_json_file(paths.state, "existing-state-malformed")
        return {
            "status": "ALREADY_INITIALIZED",
            "project_id": manifest["project_id"],
            "workflow_id": manifest["workflow_id"],
            "work_item": manifest["work_item"],
            "manifest_path": str(manifest_path),
            "manifest_digest": manifest_digest(manifest),
            "workspace": str(paths.workspace),
            "inbox": str(paths.inbox),
            "state_path": str(paths.state),
            "receipts_path": str(paths.receipts),
            "stop_path": str(paths.stop),
            "stop_present": paths.stop.is_file(),
            "state_revision": state.get("state_revision"),
            "executor_catalog": list(manifest["allowed_executor_operations"]),
            "created": False,
        }

    # Creation phase begins only after all authority/path/identity validation.
    paths.workspace.mkdir(parents=True, exist_ok=True)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.receipts.parent.mkdir(parents=True, exist_ok=True)
    for value in manifest["destinations"].values():
        (paths.artifacts_root / Path(str(value))).mkdir(parents=True, exist_ok=True)

    if manifest_path.exists():
        existing_manifest = _load_json_file(manifest_path, "existing-manifest-malformed")
        if existing_manifest != manifest:
            raise BootstrapError("existing-manifest-incompatible")
    else:
        atomic_write_json(manifest_path, manifest)

    if not paths.receipts.exists():
        paths.receipts.touch(exist_ok=False)

    if not paths.state.exists():
        store = StateStore(paths.state, manifest)
        state = store.initial()
        store.save(state)
    else:
        state = _load_json_file(paths.state, "existing-state-malformed")

    return {
        "status": "INITIALIZED",
        "project_id": manifest["project_id"],
        "workflow_id": manifest["workflow_id"],
        "work_item": manifest["work_item"],
        "manifest_path": str(manifest_path),
        "manifest_digest": manifest_digest(manifest),
        "workspace": str(paths.workspace),
        "inbox": str(paths.inbox),
        "state_path": str(paths.state),
        "receipts_path": str(paths.receipts),
        "stop_path": str(paths.stop),
        "stop_present": paths.stop.is_file(),
        "state_revision": state.get("state_revision"),
        "executor_catalog": list(manifest["allowed_executor_operations"]),
        "created": True,
    }
