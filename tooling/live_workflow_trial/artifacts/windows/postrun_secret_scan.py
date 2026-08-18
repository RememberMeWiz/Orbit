from __future__ import annotations

import argparse
import json
from pathlib import Path

from windows.qa_observability import TRACE_CANARIES, scan_canaries


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-run scan of bounded native evidence sinks for fixture canaries.")
    parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args()
    root = Path(args.evidence_dir)
    paths = [p for p in root.rglob("*") if p.is_file() and p.name != "postrun_secret_scan.json"]
    result = scan_canaries(paths, TRACE_CANARIES)
    (root / "postrun_secret_scan.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    if result["status"] != "PASS":
        print("Post-run trace/secret scan FAIL")
        return 2
    print(f"Post-run trace/secret scan PASS across {len(result['scanned_files'])} evidence files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
