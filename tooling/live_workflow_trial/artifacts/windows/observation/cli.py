from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.runtime import assert_expected_identity, resolve_runtime_paths
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler


def main() -> int:
    parser = argparse.ArgumentParser(description="Orbit bounded Windows workflow reconciler")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--project-id")
    parser.add_argument("--workflow-id")
    parser.add_argument("--work-item")
    parser.add_argument("--polls", type=int, default=2)
    parser.add_argument("--interval", type=float, default=0.30)
    args = parser.parse_args()

    manifest = load_manifest(args.root, args.manifest)
    assert_expected_identity(
        manifest,
        project_id=args.project_id,
        workflow_id=args.workflow_id,
        work_item=args.work_item,
    )
    runtime_paths = resolve_runtime_paths(args.root, manifest)
    runtime_paths.workspace.mkdir(parents=True, exist_ok=True)

    executor = PlacePacketExecutor(args.root, manifest)
    engine = WorkflowEngine(args.root, manifest, executor)
    reconciler = WorkspaceReconciler(args.root, manifest, engine)
    for i in range(max(1, args.polls)):
        for result in reconciler.scan_once():
            print(json.dumps(result, sort_keys=True))
        if i + 1 < args.polls:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
