from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# Add artifacts root to path
artifact_root = Path(__file__).resolve().parent / "artifacts"
sys.path.insert(0, str(artifact_root))

from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler


def main() -> int:
    parser = argparse.ArgumentParser(description="Orbit Live Trial Workflow Runner")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-iterations", type=int, default=0)
    args = parser.parse_args()

    manifest = load_manifest(args.root)
    workspace = args.root / "artifacts" / "sample_workspace"
    stop_file = workspace / "STOP"
    log_file = workspace / "runner.log"

    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "inbox").mkdir(parents=True, exist_ok=True)
    (workspace / "receipts").mkdir(parents=True, exist_ok=True)
    for dest in manifest.get("destinations", {}).values():
        (args.root / "artifacts" / dest).mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

    logging.info("Starting Orbit Bounded Live Workflow Runner (MVP Trial)...")
    logging.info(f"Project ID: {manifest.get('project_id')}, Workflow: {manifest.get('workflow_id')}")
    logging.info(f"Watched Inbox: {workspace / 'inbox'}")
    logging.info(f"Allowed Executor Operations: {manifest.get('allowed_executor_operations')}")
    logging.info(f"STOP Control File: {stop_file}")

    executor = PlacePacketExecutor(args.root, manifest)
    engine = WorkflowEngine(args.root, manifest, executor)
    reconciler = WorkspaceReconciler(args.root, manifest, engine)

    state = engine.store.load()
    logging.info(f"Initial Workflow State: current_stage={state.get('current_stage')}, current_owner_role={state.get('current_owner_role')}, state_revision={state.get('state_revision')}")

    iteration = 0
    while True:
        iteration += 1
        if stop_file.exists():
            logging.warning(f"STOP file detected at {stop_file}. Halting automatic advancement.")
        else:
            try:
                results = reconciler.scan_once()
                for r in results:
                    logging.info(f"Processed handoff: result={r.get('result')}, transition={r.get('transition')}, destination={r.get('destination')}")
            except Exception as e:
                logging.error(f"Error during reconciliation scan: {e}", exc_info=True)

        if args.max_iterations > 0 and iteration >= args.max_iterations:
            logging.info(f"Reached max iterations ({args.max_iterations}). Completed polling cycle.")
            break

        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
