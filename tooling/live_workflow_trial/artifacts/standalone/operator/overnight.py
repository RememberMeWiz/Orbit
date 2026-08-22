"""Overnight unattended workflow supervisor for Orbit.

Runs continuously until explicitly stopped, machine shutdown, or a
fail-closed terminal condition.

Key behaviors:
1. Restores existing active lanes from disk on startup.
2. Verifies ChatGPT accessibility and surface readiness.
3. Iteratively cycles all safe lanes.
4. Picks up new PM directives and executes authorized dispatches.
5. Collects and validates worker handoffs.
6. Reports results and blockers to Orbit PM.
7. Logs structured events to disk (events.jsonl, overnight.log).
8. Avoids noisy repeated notifications when states are unchanged.
"""
from __future__ import annotations

import json
import os
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .lane import STATE_BLOCKED, STATE_COMPLETED, STATE_HOLD, STATE_STOPPED
from .supervisor import MultiWorkItemSupervisor
from .humanpresence import DEFAULT_IDLE_SECONDS, presence
from .supervisor_process import (Heartbeat, clear_drain, code_fingerprint,
                                 drain_requested, process_identity)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OvernightRunner:
    """Unattended overnight supervisor loop."""

    def __init__(
        self,
        supervisor: MultiWorkItemSupervisor,
        *,
        poll_interval: float = 15.0,
        max_cycles: Optional[int] = None,
        idle_threshold: float = DEFAULT_IDLE_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.supervisor = supervisor
        self.poll_interval = poll_interval
        self.max_cycles = max_cycles
        self._sleep = sleeper
        self._now = clock
        self._running = False
        self._last_state_snapshot: Dict[str, str] = {}

        self.idle_threshold = idle_threshold
        self.log_file = self.supervisor.state_dir / "overnight.log"
        self.events_file = self.supervisor.state_dir / "events.jsonl"

    def _set_title(self, what: str) -> None:
        """Say what Orbit is doing, in the console title.

        The window is the thing a person actually glances at, and a title is
        visible from the taskbar without switching to it. Costs nothing and
        needs no notification dependency.
        """
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.kernel32.SetConsoleTitleW(f"Orbit — {what}")
        except Exception:
            pass

    def _new_heartbeat(self) -> Heartbeat:
        repo_root = Path(__file__).resolve().parents[2]
        beat = Heartbeat(state_root=self.supervisor.state_dir, repo_root=repo_root)
        beat.pid = os.getpid()
        identity = process_identity(beat.pid) or {}
        # Creation time as well as pid: Windows reuses process ids, so the pair
        # is the identity and the id alone is a guess.
        beat.process_creation_time = str(identity.get("created", ""))
        beat.code_fingerprint = code_fingerprint(repo_root)
        beat.branch, beat.git_sha = self._git_identity(repo_root)
        beat.started_at = utc_now_iso()
        beat.health = "STARTING"
        return beat

    @staticmethod
    def _git_identity(repo_root: Path):
        import subprocess
        def run(*args):
            try:
                out = subprocess.run(["git", *args], cwd=str(repo_root),
                                     capture_output=True, text=True, timeout=20)
                return (out.stdout or "").strip()
            except (OSError, subprocess.SubprocessError):
                return ""
        return run("rev-parse", "--abbrev-ref", "HEAD"), run("rev-parse", "HEAD")

    def _refresh_counts(self) -> None:
        """Lane counts without stepping anything. Safe to call while idle."""
        self.supervisor.refresh_lanes()
        lanes = self.supervisor.list_lanes()
        self._beat.lane_count = len(lanes)
        self._beat.active_lane_count = sum(
            1 for l in lanes if l.record.work_state not in
            (STATE_COMPLETED, STATE_BLOCKED, STATE_STOPPED, STATE_HOLD))
        self._beat.blocked_lane_count = sum(
            1 for l in lanes if l.record.work_state == STATE_BLOCKED)

    def _record_cycle(self, cycle_results) -> None:
        lanes = self.supervisor.list_lanes()
        self._beat.lane_count = len(lanes)
        self._beat.active_lane_count = sum(
            1 for l in lanes if l.record.work_state not in
            (STATE_COMPLETED, STATE_BLOCKED, STATE_STOPPED, STATE_HOLD))
        self._beat.blocked_lane_count = sum(
            1 for l in lanes if l.record.work_state == STATE_BLOCKED)
        meaningful = [r for r in cycle_results
                      if r.get("action") not in ("IDLE", "NO_OP", "AWAITING_WORKER_RESPONSE",
                                                 "AWAITING_PM_DIRECTIVE", "WAITING_FOR_TURN")]
        if meaningful:
            latest = meaningful[-1]
            self._beat.last_meaningful_action = (
                f"{latest.get('work_item', '')}:{latest.get('action', '')}")
            self._beat.current_lane = str(latest.get("work_item", ""))
        self._beat.last_cycle_completed_at = utc_now_iso()
        self._beat.health = "READY"
        self._beat.write()

    def log_event(self, event_type: str, data: Dict[str, Any], level: str = "INFO") -> None:
        timestamp = utc_now_iso()
        entry = {"timestamp": timestamp, "type": event_type, "level": level, **data}
        # Append structured JSONL event
        with self.events_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

        # Append human-readable log line
        log_line = f"[{timestamp}] [{level}] {event_type}: {json.dumps(data, default=str)}\n"
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(log_line)
        print(log_line.strip(), flush=True)

    def run(self) -> Dict[str, Any]:
        """Main overnight execution loop."""
        self._running = True
        self.log_event("OVERNIGHT_STARTED", {
            "state_dir": str(self.supervisor.state_dir),
            "poll_interval": self.poll_interval,
            "max_cycles": self.max_cycles,
        })

        self._beat = self._new_heartbeat()
        self._beat.write()

        # A surface that is not ready yet is not a reason to give up.
        #
        # This used to return immediately, so starting Orbit while ChatGPT was
        # still coming up -- or while it showed a sign-in screen -- exited the
        # supervisor entirely and left every lane unattended until a human
        # noticed. The whole point of unattended operation is surviving exactly
        # that, so the surface is re-checked each cycle and the loop waits.
        surface = self.supervisor.check_surface(allow_launch=True)
        self.log_event("SURFACE_CHECK", surface)
        if not surface.get("drivable", False):
            self.log_event("SURFACE_UNAVAILABLE_WAITING", surface, level="WARNING")
            self._beat.health = "WAITING_FOR_SURFACE"
            self._beat.write()

        cycle_count = 0
        try:
            while self._running:
                if self.supervisor.stopped():
                    self.log_event("OVERNIGHT_STOPPED", {"reason": "global-stop-active"})
                    break

                # Supervisor-control drain, distinct from workflow STOP: asking
                # this process to exit is not the same as halting all work, and
                # conflating them makes restarting Orbit look like stopping it.
                if drain_requested(self.supervisor.state_dir):
                    self.log_event("OVERNIGHT_DRAINED", {"reason": "drain-requested"})
                    self._beat.health = "DRAINED"
                    self._beat.write()
                    clear_drain(self.supervisor.state_dir)
                    break

                # Checked before incrementing, and before the yield and
                # surface-wait paths that `continue` past the bottom of the
                # loop: a bounded run with someone at the keyboard otherwise
                # spins forever, and counting the aborted pass overstates by one.
                if self.max_cycles is not None and cycle_count >= self.max_cycles:
                    self.log_event("MAX_CYCLES_REACHED", {"cycles": cycle_count})
                    break

                cycle_count += 1

                self._beat.last_cycle_started_at = utc_now_iso()
                self._beat.write()

                # Yield the machine to whoever is sitting at it.
                #
                # Orbit switches the visible conversation and writes into the
                # composer, so running while someone is typing changes what is
                # on screen under their hands and interleaves their input with
                # Orbit's. Checked before the surface, because even asking the
                # app about itself is unnecessary if we are not going to act.
                who = presence(self.idle_threshold)
                if who.present:
                    # Lane counts are refreshed even while standing down.
                    # `orbit supervisor status` is what a person reads to decide
                    # whether Orbit is stuck, and reporting zero lanes because
                    # we skipped the cycle reads as "it lost all the work".
                    self._refresh_counts()
                    self._beat.health = "YIELDING_TO_HUMAN"
                    self._beat.write()
                    self._set_title(f"idle - you are using the machine "
                                    f"({who.idle_seconds:.0f}s since input)")
                    self._sleep(min(self.poll_interval, 10.0))
                    continue

                # Re-checked every cycle, not only at startup.
                self._set_title("checking ChatGPT window")
                surface = self.supervisor.check_surface(allow_launch=True)
                if not surface.get("drivable", False):
                    self._refresh_counts()
                    self._beat.health = "WAITING_FOR_SURFACE"
                    self._beat.write()
                    self._set_title(f"waiting for ChatGPT ({surface.get('reason_code', '')})")
                    self.log_event("SURFACE_UNAVAILABLE_WAITING", surface, level="WARNING")
                    self._sleep(self.poll_interval)
                    continue
                self._beat.last_successful_surface_check = utc_now_iso()
                lanes = self.supervisor.list_lanes()
                active_lanes = [l for l in lanes if not l.stopped() and not l.paused() and l.record.work_state not in (STATE_COMPLETED, STATE_BLOCKED, STATE_HOLD)]

                # Execute one pass over all active lanes
                self._set_title(f"working - cycle {cycle_count}")
                cycle_results = self.supervisor.cycle_all()

                # Check for state changes to log meaningfully
                for res in cycle_results:
                    w_item = res.get("work_item", "")
                    curr_state = res.get("state", "")
                    prev_state = self._last_state_snapshot.get(w_item, "")
                    if curr_state != prev_state or res.get("action") not in ("NO_OP", "IDLE"):
                        self._last_state_snapshot[w_item] = curr_state
                        self.log_event("LANE_TRANSITION", res)

                self._record_cycle(cycle_results)

                self._sleep(self.poll_interval)
        except KeyboardInterrupt:
            self.log_event("OVERNIGHT_INTERRUPTED", {"reason": "keyboard-interrupt"})
        except Exception as e:
            self.log_event("OVERNIGHT_ERROR", {"error": str(e), "type": type(e).__name__}, level="ERROR")
            return {
                "ok": False,
                "status": "ERROR",
                "error": str(e),
                "cycles": cycle_count,
            }
        finally:
            self._running = False
            self._set_title("stopped")
            self.log_event("OVERNIGHT_FINISHED", {"total_cycles": cycle_count})

        summary = self.supervisor.status_summary()
        return {
            "ok": True,
            "status": "COMPLETED",
            "cycles": cycle_count,
            "summary": summary,
        }
