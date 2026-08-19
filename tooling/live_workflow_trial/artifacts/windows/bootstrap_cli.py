from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workflow.core.bootstrap import BootstrapError, bootstrap_workspace
from workflow.core.storage import is_within


def _load_trusted_config(root: Path, config: Path) -> dict:
    artifacts_root = (root / "artifacts").resolve()
    path = config if config.is_absolute() else root / config
    try:
        resolved = path.resolve(strict=True)
    except (OSError, FileNotFoundError) as exc:
        raise BootstrapError("bootstrap-config-not-found") from exc
    if not is_within(resolved, artifacts_root):
        raise BootstrapError("bootstrap-config-outside-artifacts-root")
    if resolved.is_symlink():
        raise BootstrapError("bootstrap-config-reparse-point-not-allowed")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("bootstrap-config-malformed") from exc
    if not isinstance(value, dict):
        raise BootstrapError("bootstrap-config-not-object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Orbit bounded real-work-item bootstrapper")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--work-item", required=True)
    args = parser.parse_args()

    try:
        manifest = _load_trusted_config(args.root, args.config)
        result = bootstrap_workspace(
            args.root,
            manifest,
            project_id=args.project_id,
            workflow_id=args.workflow_id,
            work_item=args.work_item,
        )
    except BootstrapError as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
