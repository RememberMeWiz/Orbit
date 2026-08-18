from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler


def main() -> int:
    parser = argparse.ArgumentParser(description="Orbit bounded Windows workflow reconciler")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--polls", type=int, default=2)
    parser.add_argument("--interval", type=float, default=0.30)
    args = parser.parse_args()

    manifest = load_manifest(args.root)
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
