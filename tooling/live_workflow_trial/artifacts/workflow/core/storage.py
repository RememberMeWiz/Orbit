from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict


def utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    """Write JSON through a same-directory temp file then atomically replace.

    Same-directory replacement keeps the operation on one filesystem/volume, which
    is the required boundary for os.replace/Path.replace atomicity semantics.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True).encode("utf-8")
    with temp.open("wb") as f:
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
