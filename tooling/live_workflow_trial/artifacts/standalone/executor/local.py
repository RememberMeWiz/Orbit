"""Typed local executor.

Deny-by-default, capability-bound, root-confined. It mirrors the path discipline
the accepted PLACE_PACKET adapter already enforces:

* the caller supplies a *relative* path; absolute paths and ``..`` are refused
  before any filesystem call;
* the candidate is resolved and must land inside the approved root;
* every existing component is checked for a reparse point (symlink or Windows
  junction), so a link cannot be used to step outside;
* the path is re-checked *after* resolution, closing the window between deciding
  and acting.

Only read-only operations are implemented. Everything else in the operation table
exists as a declared shape and returns ``operation-not-enabled``, so the gate is
visible in the audit trail rather than being an absence.
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Dict, Iterable, List, Optional

from workflow.core.storage import is_within

from .contracts import (
    IMPLEMENTED_OPERATIONS,
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    OPERATIONS_BY_NAME,
    ExecutorRequest,
    ExecutorResult,
)


def is_reparse(path: Path) -> bool:
    """True for a symlink or Windows junction. Errors count as reparse."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def path_has_reparse(root: Path, candidate: Path) -> bool:
    """Check the root and every existing component between it and the candidate."""
    if is_reparse(root):
        return True
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and is_reparse(current):
            return True
    return False


class TypedLocalExecutor:
    """Executes typed operations for a role, bounded by that role's capabilities."""

    def __init__(
        self,
        approved_root: Path,
        *,
        capabilities_for_role: Callable[[str], Iterable[str]],
        audit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.approved_root = Path(approved_root).resolve()
        self._capabilities_for_role = capabilities_for_role
        self._audit = audit

    # -- path safety -----------------------------------------------------

    def _resolve(self, raw: str) -> tuple[Optional[Path], str]:
        """Resolve a caller-supplied relative path, or return a denial reason."""
        text = str(raw or "").strip()
        if not text:
            return None, "path-missing"

        # Reject on both path flavours: a POSIX-looking string can still be an
        # absolute Windows path, and vice versa.
        #
        # `root` matters as much as `is_absolute`. On Windows "/etc/passwd" is
        # rooted but not absolute (it has no drive), and joining it onto the
        # approved root silently discards everything below the drive letter --
        # pathlib's absolute-join trap. Treat anything rooted as absolute.
        win = PureWindowsPath(text)
        native = Path(text)
        if win.is_absolute() or win.drive or win.root or native.is_absolute() or native.root:
            return None, "path-absolute-not-allowed"
        if ".." in win.parts or ".." in native.parts:
            return None, "path-parent-traversal-not-allowed"

        unresolved = self.approved_root / native
        if path_has_reparse(self.approved_root, unresolved):
            return None, "path-reparse-point-not-allowed"
        try:
            resolved = unresolved.resolve()
        except OSError:
            return None, "path-resolution-failed"
        if not is_within(resolved, self.approved_root):
            return None, "path-outside-approved-root"

        # Re-check after resolution: resolve() follows links, so a component
        # could have pointed outside even though the pre-check passed.
        if path_has_reparse(self.approved_root, resolved):
            return None, "path-reparse-point-not-allowed"
        return resolved, ""

    # -- dispatch --------------------------------------------------------

    def execute(self, request: ExecutorRequest) -> ExecutorResult:
        result = self._execute(request)
        if self._audit is not None:
            self._audit({
                "request": request.to_dict(),
                "result": result.to_dict(),
            })
        return result

    def _execute(self, request: ExecutorRequest) -> ExecutorResult:
        spec = OPERATIONS_BY_NAME[request.operation]

        granted = set(self._capabilities_for_role(request.role) or ())
        if request.operation not in granted:
            return ExecutorResult.deny(
                request.operation, "capability-not-granted",
                f"role {request.role} was not granted {request.operation}",
            )

        if not spec.implemented:
            return ExecutorResult.deny(
                request.operation, "operation-not-enabled",
                spec.gate or "operation is declared but not enabled",
            )
        if request.operation not in IMPLEMENTED_OPERATIONS:
            return ExecutorResult.deny(request.operation, "operation-not-enabled")

        resolved, reason = self._resolve(request.path)
        if resolved is None:
            return ExecutorResult.deny(request.operation, reason)

        handler = {
            "READ_FILE": self._read_file,
            "LIST_DIRECTORY": self._list_directory,
            "STAT_PATH": self._stat_path,
        }[request.operation]
        return handler(request, resolved)

    # -- operations ------------------------------------------------------

    def _read_file(self, request: ExecutorRequest, path: Path) -> ExecutorResult:
        if not path.is_file():
            return ExecutorResult.deny(request.operation, "path-not-a-file")
        try:
            size = path.stat().st_size
        except OSError:
            return ExecutorResult.deny(request.operation, "path-stat-failed")
        if size > MAX_READ_BYTES:
            return ExecutorResult.deny(
                request.operation, "file-too-large",
                f"{size} bytes exceeds cap of {MAX_READ_BYTES}",
            )
        try:
            data = path.read_bytes()
        except OSError:
            return ExecutorResult.deny(request.operation, "file-unreadable")
        # Re-check size after reading: the file could have grown between the
        # stat and the read.
        if len(data) > MAX_READ_BYTES:
            return ExecutorResult.deny(request.operation, "file-too-large")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ExecutorResult.deny(request.operation, "file-not-utf8")
        return ExecutorResult.allow(
            request.operation,
            {"text": text, "size_bytes": len(data)},
            resolved_path=str(path),
        )

    def _list_directory(self, request: ExecutorRequest, path: Path) -> ExecutorResult:
        if not path.is_dir():
            return ExecutorResult.deny(request.operation, "path-not-a-directory")
        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(path.iterdir())
        except OSError:
            return ExecutorResult.deny(request.operation, "directory-unreadable")
        truncated = len(children) > MAX_LIST_ENTRIES
        for child in children[:MAX_LIST_ENTRIES]:
            # A link inside the directory is reported but never followed, so a
            # listing cannot become a way to learn about paths outside the root.
            entries.append({
                "name": child.name,
                "kind": "link" if is_reparse(child) else ("dir" if child.is_dir() else "file"),
            })
        return ExecutorResult.allow(
            request.operation,
            {"entries": entries, "truncated": truncated},
            resolved_path=str(path),
        )

    def _stat_path(self, request: ExecutorRequest, path: Path) -> ExecutorResult:
        if not path.exists():
            return ExecutorResult.allow(request.operation, {"exists": False}, resolved_path=str(path))
        try:
            stat = path.stat()
        except OSError:
            return ExecutorResult.deny(request.operation, "path-stat-failed")
        return ExecutorResult.allow(
            request.operation,
            {
                "exists": True,
                "kind": "dir" if path.is_dir() else "file",
                "size_bytes": stat.st_size,
            },
            resolved_path=str(path),
        )
