from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Add artifacts root to path
artifact_root = Path(__file__).resolve().parent / "artifacts"
sys.path.insert(0, str(artifact_root))

from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.runtime import (
    RuntimeConfigurationError,
    assert_expected_identity,
    resolve_runtime_paths,
)
from windows.adapters.place_packet import PlacePacketExecutor
from windows.observation.reconciler import WorkspaceReconciler


def main() -> int:
    parser = argparse.ArgumentParser(description="Orbit Live Trial Workflow Runner")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Workflow manifest to bind to. Defaults to artifacts/workflow_manifest.json. "
             "Point this at a bootstrapped workspace manifest to run a real work item.",
    )
    parser.add_argument("--project-id", help="Expected project id; refuses to start on mismatch.")
    parser.add_argument("--workflow-id", help="Expected workflow id; refuses to start on mismatch.")
    parser.add_argument("--work-item", help="Expected work item; refuses to start on mismatch.")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-iterations", type=int, default=0)
    args = parser.parse_args()

    # Resolve configuration and bind launch identity before creating anything.
    # A manifest that disagrees with the operator's stated identity fails closed
    # rather than quietly driving the wrong work item.
    try:
        manifest = load_manifest(args.root, args.manifest)
        assert_expected_identity(
            manifest,
            project_id=args.project_id,
            workflow_id=args.workflow_id,
            work_item=args.work_item,
        )
        paths = resolve_runtime_paths(args.root, manifest)
    except RuntimeConfigurationError as exc:
        print(f"orbit-runner-configuration-rejected: {exc}", file=sys.stderr)
        return 2

    log_file = paths.workspace / "runner.log"

    paths.workspace.mkdir(parents=True, exist_ok=True)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    paths.receipts.parent.mkdir(parents=True, exist_ok=True)
    for dest in manifest.get("destinations", {}).values():
        (paths.artifacts_root / dest).mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )

    logging.info("Starting Orbit Bounded Live Workflow Runner (MVP Trial)...")
    logging.info(
        f"Project ID: {manifest.get('project_id')}, Workflow: {manifest.get('workflow_id')}, "
        f"Work Item: {manifest.get('work_item')}"
    )
    logging.info(f"Workspace: {paths.workspace}")
    logging.info(f"Watched Inbox: {paths.inbox}")
    logging.info(f"Allowed Executor Operations: {manifest.get('allowed_executor_operations')}")
    logging.info(f"STOP Control File: {paths.stop}")

    executor = PlacePacketExecutor(args.root, manifest)
    engine = WorkflowEngine(args.root, manifest, executor)
    reconciler = WorkspaceReconciler(args.root, manifest, engine)

    state = engine.store.load()
    logging.info(f"Initial Workflow State: current_stage={state.get('current_stage')}, current_owner_role={state.get('current_owner_role')}, state_revision={state.get('state_revision')}")

    iteration = 0
    stopped = None
    while True:
        iteration += 1

        # STOP is enforced inside the reconciler, which is the single authority
        # shared with the one-shot CLI. The runner only reports transitions so a
        # long poll loop does not repeat the same line every interval.
        currently_stopped = reconciler.is_stopped()
        if currently_stopped != stopped:
            if currently_stopped:
                logging.warning(f"STOP file detected at {paths.stop}. Halting automatic advancement.")
            elif stopped is not None:
                logging.info(f"STOP file cleared at {paths.stop}. Resuming automatic advancement.")
            stopped = currently_stopped

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
