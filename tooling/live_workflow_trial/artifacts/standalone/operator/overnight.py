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
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .lane import STATE_BLOCKED, STATE_COMPLETED, STATE_STOPPED
from .supervisor import MultiWorkItemSupervisor


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

        self.log_file = self.supervisor.state_dir / "overnight.log"
        self.events_file = self.supervisor.state_dir / "events.jsonl"

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

        # Ensure surface is drivable
        surface = self.supervisor.check_surface(allow_launch=True)
        self.log_event("SURFACE_CHECK", surface)
        if not surface.get("drivable", False):
            self.log_event("SURFACE_UNAVAILABLE", surface, level="ERROR")
            self._running = False
            return {
                "ok": False,
                "status": "SURFACE_UNAVAILABLE",
                "surface": surface,
                "cycles": 0,
            }

        cycle_count = 0
        try:
            while self._running:
                if self.supervisor.stopped():
                    self.log_event("OVERNIGHT_STOPPED", {"reason": "global-stop-active"})
                    break

                cycle_count += 1
                lanes = self.supervisor.list_lanes()
                active_lanes = [l for l in lanes if not l.stopped() and not l.paused() and l.record.work_state not in (STATE_COMPLETED, STATE_BLOCKED)]

                # Execute one pass over all active lanes
                cycle_results = self.supervisor.cycle_all()

                # Check for state changes to log meaningfully
                for res in cycle_results:
                    w_item = res.get("work_item", "")
                    curr_state = res.get("state", "")
                    prev_state = self._last_state_snapshot.get(w_item, "")
                    if curr_state != prev_state or res.get("action") not in ("NO_OP", "IDLE"):
                        self._last_state_snapshot[w_item] = curr_state
                        self.log_event("LANE_TRANSITION", res)

                if self.max_cycles is not None and cycle_count >= self.max_cycles:
                    self.log_event("MAX_CYCLES_REACHED", {"cycles": cycle_count})
                    break

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
            self.log_event("OVERNIGHT_FINISHED", {"total_cycles": cycle_count})

        summary = self.supervisor.status_summary()
        return {
            "ok": True,
            "status": "COMPLETED",
            "cycles": cycle_count,
            "summary": summary,
        }
