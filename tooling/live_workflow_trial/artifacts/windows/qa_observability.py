from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List

FIXTURE_VERSION = "orbit.nwin-fixtures/0.2"
TRACE_CANARIES = {
    "handoff_body": "ORBIT_CANARY_BODY_91f4e12a",
    "malformed_path_text": "ORBIT_CANARY_PATH_62a337df",
    "simulated_credential": "ORBIT_CANARY_CRED_7bbcc946",
    "archive_member": "ORBIT_CANARY_ARCHIVE_2c0129a1",
}


def evidence_directory() -> Path | None:
    raw = os.environ.get("ORBIT_NATIVE_EVIDENCE_DIR", "").strip()
    return Path(raw) if raw else None


def write_gate_evidence(gate_id: str, payload: Dict[str, Any]) -> None:
    root = evidence_directory()
    if root is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    record = dict(payload)
    record.setdefault("gate_id", gate_id)
    record.setdefault("fixture_version", FIXTURE_VERSION)
    out = root / f"{gate_id}.json"
    out.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def read_receipts(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    result: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            result.append(json.loads(line))
    return result


def scan_canaries(paths: Iterable[Path], canaries: Dict[str, str]) -> Dict[str, Any]:
    """Scan bounded trace/evidence sinks without echoing secret values.

    The report contains canary labels and SHA-256 fingerprints only. Raw canary
    values are intentionally excluded from evidence output.
    """
    hits: Dict[str, list[str]] = {label: [] for label in canaries}
    scanned: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        scanned.append(str(path))
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for label, secret in canaries.items():
            if secret.encode("utf-8") in data:
                hits[label].append(str(path))
    fingerprints = {
        label: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for label, value in canaries.items()
    }
    return {
        "status": "PASS" if all(not value for value in hits.values()) else "FAIL",
        "scanned_files": scanned,
        "canary_sha256": fingerprints,
        "hits_by_label": hits,
        "raw_canaries_emitted": False,
    }
