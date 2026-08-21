"""Is a supervisor running, is it the only one, and is it running this code?

Three questions an unattended operator has to be able to answer, and a PID
answers none of them:

* Windows reuses PIDs, so a live process with the recorded id may be something
  else entirely. The heartbeat therefore records process *creation time* as well,
  which together with the id is a stable identity.
* A file saying "running" proves only that something once wrote a file. Liveness
  is a fresh heartbeat plus a matching live process.
* **A running Python process holds its code in memory.** Editing files on disk
  does not change what it is executing, so a supervisor started before a fix
  keeps running the bug while the checkout looks correct. The heartbeat records
  the code fingerprint it started with, and a mismatch is reported as OUTDATED
  rather than quietly assumed current.

Single-instance is enforced with a Windows named mutex — the same primitive and
the same reasoning as the delivery lock: no lease to expire underneath a slow
holder, and the kernel releases it when the process dies.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from workflow.core.storage import atomic_write_json, utc_now_iso

HEARTBEAT_SCHEMA = "orbit.supervisor-heartbeat/0.1"
HEARTBEAT_NAME = "supervisor.heartbeat.json"
DRAIN_NAME = "SUPERVISOR_DRAIN"

# How stale a heartbeat may be before the supervisor is presumed dead. Generous
# on purpose: a cycle that is waiting on the shared window legitimately takes a
# while, and declaring a live supervisor dead is worse than noticing late.
STALE_AFTER_SECONDS = 300.0


def heartbeat_path(state_root: Path) -> Path:
    return Path(state_root) / HEARTBEAT_NAME


def drain_path(state_root: Path) -> Path:
    """Supervisor-control stop. Deliberately *not* the workflow STOP file.

    Asking the operator process to exit is a different act from halting the
    workflow, and conflating them means restarting Orbit looks identical to
    stopping all work.
    """
    return Path(state_root) / DRAIN_NAME


def code_fingerprint(repo_root: Path) -> str:
    """What code a process is actually running.

    Git SHA alone is not enough: the working tree is usually ahead of the last
    commit while work is in progress, and that is exactly when a stale process
    matters most. So the fingerprint hashes the operator and bridge sources
    themselves.
    """
    root = Path(repo_root)
    digest = hashlib.sha256()
    for folder in ("standalone/operator", "standalone/bridge"):
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")) + sorted(base.rglob("*.ps1")):
            digest.update(path.name.encode("utf-8"))
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
    return digest.hexdigest()[:16]


def process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Live process facts, or None. Creation time disambiguates a reused PID."""
    if sys.platform != "win32":
        return None
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' -ErrorAction SilentlyContinue; "
             "if ($p) { @{ pid = $p.ProcessId; created = $p.CreationDate.ToString('o'); "
             "cmd = $p.CommandLine } | ConvertTo-Json -Compress }"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (completed.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


@dataclass
class Heartbeat:
    state_root: Path
    repo_root: Path
    pid: int = 0
    process_creation_time: str = ""
    branch: str = ""
    git_sha: str = ""
    code_fingerprint: str = ""
    started_at: str = ""
    last_cycle_started_at: str = ""
    last_cycle_completed_at: str = ""
    last_successful_surface_check: str = ""
    last_meaningful_action: str = ""
    lane_count: int = 0
    active_lane_count: int = 0
    blocked_lane_count: int = 0
    current_lane: str = ""
    health: str = "STARTING"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": HEARTBEAT_SCHEMA,
            "state_root": str(self.state_root),
            "repo_root": str(self.repo_root),
            "pid": self.pid,
            "process_creation_time": self.process_creation_time,
            "branch": self.branch,
            "git_sha": self.git_sha,
            "code_fingerprint": self.code_fingerprint,
            "started_at": self.started_at,
            "last_cycle_started_at": self.last_cycle_started_at,
            "last_cycle_completed_at": self.last_cycle_completed_at,
            "last_successful_surface_check": self.last_successful_surface_check,
            "last_meaningful_action": self.last_meaningful_action,
            "lane_count": self.lane_count,
            "active_lane_count": self.active_lane_count,
            "blocked_lane_count": self.blocked_lane_count,
            "current_lane": self.current_lane,
            "health": self.health,
            "updated_at": utc_now_iso(),
        }

    def write(self) -> None:
        atomic_write_json(heartbeat_path(self.state_root), self.to_dict())


def read_heartbeat(state_root: Path) -> Optional[Dict[str, Any]]:
    path = heartbeat_path(state_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _age_seconds(stamp: str) -> Optional[float]:
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def supervisor_status(state_root: Path, repo_root: Path) -> Dict[str, Any]:
    """Everything needed to decide whether to start, wait, or restart."""
    beat = read_heartbeat(state_root)
    current_code = code_fingerprint(repo_root)

    if beat is None:
        return {"running": False, "health": "ABSENT", "reason_code": "no-heartbeat",
                "current_code_fingerprint": current_code}

    age = _age_seconds(beat.get("updated_at", ""))
    live = process_identity(int(beat.get("pid") or 0)) if beat.get("pid") else None

    # A live process whose creation time differs is a *different* process that
    # happens to have inherited the id.
    same_process = bool(live) and (
        not beat.get("process_creation_time")
        or str(live.get("created", ""))[:19] == str(beat.get("process_creation_time"))[:19])

    status = {
        "running": bool(live) and same_process,
        "pid": beat.get("pid"),
        "process_creation_time": beat.get("process_creation_time"),
        "heartbeat_age_seconds": age,
        "heartbeat_path": str(heartbeat_path(state_root)),
        "heartbeat_code_fingerprint": beat.get("code_fingerprint"),
        "current_code_fingerprint": current_code,
        "git_sha": beat.get("git_sha"),
        "branch": beat.get("branch"),
        "lane_count": beat.get("lane_count"),
        "active_lane_count": beat.get("active_lane_count"),
        "blocked_lane_count": beat.get("blocked_lane_count"),
        "last_meaningful_action": beat.get("last_meaningful_action"),
        "state_root": beat.get("state_root"),
    }

    if not live:
        status["health"] = "DEAD"
        status["reason_code"] = "process-not-running"
    elif not same_process:
        # Windows reuses PIDs; this is why creation time is recorded.
        status["health"] = "DEAD"
        status["reason_code"] = "pid-reused-by-another-process"
        status["running"] = False
    elif age is not None and age > STALE_AFTER_SECONDS:
        status["health"] = "STALE"
        status["reason_code"] = f"heartbeat-{int(age)}s-old"
    elif beat.get("code_fingerprint") != current_code:
        # The files changed under a process that already loaded them.
        status["health"] = "OUTDATED"
        status["reason_code"] = "running-code-differs-from-checkout"
    else:
        status["health"] = beat.get("health", "READY")
        status["reason_code"] = "ok"
    return status


def request_drain(state_root: Path, reason: str = "operator-requested") -> Dict[str, Any]:
    """Ask the supervisor to finish safely and exit. Not a workflow STOP."""
    path = drain_path(state_root)
    atomic_write_json(path, {"requested_at": utc_now_iso(), "reason": reason})
    return {"ok": True, "drain_requested": True, "path": str(path), "reason": reason}


def clear_drain(state_root: Path) -> None:
    try:
        drain_path(state_root).unlink()
    except OSError:
        pass


def drain_requested(state_root: Path) -> bool:
    return drain_path(state_root).is_file()
