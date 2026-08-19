from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .runtime import RuntimeConfigurationError, validate_manifest_authority
from .storage import is_within


def load_manifest(root: Path, manifest_path: Path | None = None) -> Dict[str, Any]:
    artifacts_root = (root / "artifacts").resolve()
    path = artifacts_root / "workflow_manifest.json" if manifest_path is None else manifest_path
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except (OSError, FileNotFoundError) as exc:
        raise RuntimeConfigurationError("manifest-not-found") from exc
    if not is_within(resolved, artifacts_root):
        raise RuntimeConfigurationError("manifest-outside-artifacts-root")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigurationError("manifest-malformed") from exc
    if not isinstance(value, dict):
        raise RuntimeConfigurationError("manifest-not-object")
    validate_manifest_authority(value)
    return value
