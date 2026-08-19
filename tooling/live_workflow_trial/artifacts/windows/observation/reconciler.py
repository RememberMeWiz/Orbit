from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from workflow.core.runtime import resolve_runtime_paths
from workflow.core.validation import NAME_RE


class StableArtifactTracker:
    def __init__(self, stable_window_seconds: float):
        self.window = stable_window_seconds
        self.seen: Dict[str, Tuple[int, int, float]] = {}
        self.first_seen: Dict[str, float] = {}
        self.last_changed: Dict[str, float] = {}
        self.stable_at: Dict[str, float] = {}
        self.processed_signatures: set[Tuple[str, int, int]] = set()
        self.processing_count: Dict[str, int] = {}

    def eligible(self, path: Path, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        key = str(path)
        prior = self.seen.get(key)
        if key not in self.first_seen:
            self.first_seen[key] = now
        if prior is None or prior[:2] != signature:
            self.seen[key] = (signature[0], signature[1], now)
            self.last_changed[key] = now
            self.stable_at.pop(key, None)
            return False
        if (key, signature[0], signature[1]) in self.processed_signatures:
            return False
        eligible = (now - prior[2]) >= self.window
        if eligible:
            self.stable_at.setdefault(key, now)
        return eligible

    def mark_processed(self, path: Path) -> None:
        stat = path.stat()
        key = str(path)
        self.processed_signatures.add((key, stat.st_size, stat.st_mtime_ns))
        self.processing_count[key] = self.processing_count.get(key, 0) + 1

    def observation(self, path: Path) -> Dict[str, Any]:
        key = str(path)
        stat = path.stat()
        return {
            "observed_path": key,
            "first_seen": self.first_seen.get(key),
            "last_changed": self.last_changed.get(key),
            "stable_at": self.stable_at.get(key),
            "processing_count": self.processing_count.get(key, 0),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }


class WorkspaceReconciler:
    def __init__(self, root: Path, manifest: Dict[str, Any], engine: Any):
        self.root = root
        self.manifest = manifest
        self.engine = engine
        self.runtime_paths = resolve_runtime_paths(root, manifest)
        self.inbox = self.runtime_paths.inbox
        self.stop_path = self.runtime_paths.stop
        self.tracker = StableArtifactTracker(float(manifest["stable_window_seconds"]))

    def is_stopped(self) -> bool:
        return self.stop_path.is_file()

    def scan_once(self, now: Optional[float] = None):
        # STOP is a configuration-owned control inside the selected workspace.
        # Presence freezes automatic advancement, including after process restart.
        if self.is_stopped():
            return []
        self.inbox.mkdir(parents=True, exist_ok=True)
        results = []
        for path in sorted(self.inbox.iterdir()):
            if not path.is_file() or not NAME_RE.match(path.name):
                continue
            if path.is_symlink():
                continue
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                continue
            if self.tracker.eligible(path, now=now):
                result = self.engine.process(path)
                self.tracker.mark_processed(path)
                result.setdefault("stability_observation", self.tracker.observation(path))
                results.append(result)
        return results
