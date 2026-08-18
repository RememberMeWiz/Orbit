from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class HeaderParseResult:
    ok: bool
    reason: str
    fields: Dict[str, str]


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str
    metadata: Optional[Dict[str, Any]] = None
