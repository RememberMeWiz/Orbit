from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def load_manifest(root: Path) -> Dict[str, Any]:
    return json.loads((root / "artifacts" / "workflow_manifest.json").read_text(encoding="utf-8"))
