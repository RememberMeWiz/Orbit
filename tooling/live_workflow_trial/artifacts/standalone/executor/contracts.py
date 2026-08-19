"""Typed local executor contracts.

Every machine action Orbit can take is a named operation with its own permission.
There is deliberately no generic ``RUN_COMMAND(string)``: an agent cannot ask for
"run this", only for a specific typed operation the calling role was granted.

Operations are declared here in one table so the full authority surface is
readable in a single place, and so an operation that exists as a *shape* but is
not yet permitted to run says so explicitly rather than silently missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


class ExecutorError(ValueError):
    """Raised when an executor request is malformed. Denials are results, not raises."""


@dataclass(frozen=True)
class OperationSpec:
    name: str
    implemented: bool
    read_only: bool
    summary: str
    gate: str = ""


# The complete authority surface. Adding a row is an Architecture/QA decision,
# never an implementation convenience.
OPERATIONS: Tuple[OperationSpec, ...] = (
    OperationSpec(
        "READ_FILE", True, True,
        "Read a UTF-8 text file inside an approved root, size-capped.",
    ),
    OperationSpec(
        "LIST_DIRECTORY", True, True,
        "List immediate children of a directory inside an approved root.",
    ),
    OperationSpec(
        "STAT_PATH", True, True,
        "Report existence, kind and size of a path inside an approved root.",
    ),
    OperationSpec(
        "WRITE_FILE_IN_APPROVED_ROOT", False, False,
        "Write arbitrary content inside an approved root.",
        gate="Broader than PLACE_PACKET, which writes only a fixed-shape packet under a "
             "digest-derived name. Needs an Architecture/QA gate before enabling.",
    ),
    OperationSpec(
        "RUN_APPROVED_PROCESS", False, False,
        "Run a pre-registered executable with pre-registered arguments.",
        gate="Process execution. Needs an Architecture/QA gate, a registry of approved "
             "executables, and an argument allowlist.",
    ),
    OperationSpec(
        "RUN_APPROVED_TEST", False, False,
        "Run the project's registered test command.",
        gate="Special case of RUN_APPROVED_PROCESS; inherits the same gate.",
    ),
    OperationSpec(
        "GIT_STATUS", False, False,
        "Report working-tree status.",
        gate="Read-only in intent, but reaches a VCS outside the approved root and can "
             "leak paths. Needs an Architecture/QA gate.",
    ),
)

OPERATIONS_BY_NAME: Dict[str, OperationSpec] = {spec.name: spec for spec in OPERATIONS}
IMPLEMENTED_OPERATIONS: Tuple[str, ...] = tuple(s.name for s in OPERATIONS if s.implemented)
READ_ONLY_OPERATIONS: Tuple[str, ...] = tuple(s.name for s in OPERATIONS if s.read_only)

# Read cap. Large enough for any handoff or manifest, small enough that a role
# cannot pull an arbitrarily large file into a model context or into memory.
MAX_READ_BYTES = 1_048_576
MAX_LIST_ENTRIES = 1_000


@dataclass(frozen=True)
class ExecutorRequest:
    """One typed request. Identity fields come from governed state, not prose."""

    operation: str
    role: str
    task_id: str
    work_item: str
    path: str = ""
    options: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.operation not in OPERATIONS_BY_NAME:
            raise ExecutorError(f"executor-operation-not-allowlisted:{self.operation}")
        for name in ("role", "task_id", "work_item"):
            if not str(getattr(self, name) or "").strip():
                raise ExecutorError(f"executor-request-missing:{name}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "role": self.role,
            "task_id": self.task_id,
            "work_item": self.work_item,
            "path": self.path,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class ExecutorResult:
    """Outcome of a typed request. A denial is an ordinary result with a reason."""

    ok: bool
    operation: str
    reason_code: str = ""
    detail: str = ""
    data: Optional[Dict[str, Any]] = None
    resolved_path: str = ""

    @staticmethod
    def deny(operation: str, reason_code: str, detail: str = "") -> "ExecutorResult":
        return ExecutorResult(ok=False, operation=operation, reason_code=reason_code, detail=detail)

    @staticmethod
    def allow(operation: str, data: Dict[str, Any], resolved_path: str = "") -> "ExecutorResult":
        return ExecutorResult(ok=True, operation=operation, reason_code="ok", data=data, resolved_path=resolved_path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "data": self.data,
            "resolved_path": self.resolved_path,
        }
