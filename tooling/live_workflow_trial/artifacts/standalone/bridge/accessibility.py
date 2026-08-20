"""Runtime guard for the ChatGPT desktop accessibility surface.

The bridge only works when the app's renderer exposes a semantic tree, which in
practice means it was started with ``--force-renderer-accessibility``. That is a
launch-time property: an app already running without it cannot be persuaded to
grow one.

So the guard has exactly two moves. If the app is not running, start it with the
flag. If it is running but unusable, say so and stop.

What it deliberately will not do is close, kill or restart a running app. The
Product Owner may be mid-conversation in that window, and an unattended process
that decides to end a human's session to unblock itself is a worse failure than
staying blocked. Every unusable-but-running case therefore ends in
NEEDS_HUMAN_RESTART with the reason attached.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .uia import UiaDriver

# Terminal outcomes.
READY = "READY"                                # semantic tree usable now
LAUNCHED = "LAUNCHED"                          # Orbit started it, now usable
NEEDS_HUMAN_RESTART = "NEEDS_HUMAN_RESTART"    # running, unusable, not ours to kill
UNAVAILABLE = "UNAVAILABLE"                    # cannot be started at all


@dataclass(frozen=True)
class GuardOutcome:
    status: str
    reason_code: str
    detail: str = ""
    remedy: str = ""
    state: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (READY, LAUNCHED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "remedy": self.remedy,
            "state": dict(self.state),
        }


RESTART_INSTRUCTION = (
    "Close ChatGPT Desktop completely, then let Orbit start it, or start it "
    "manually with --force-renderer-accessibility."
)


class AccessibilityGuard:
    def __init__(
        self,
        driver: Optional[UiaDriver] = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        settle_seconds: float = 2.0,
        launch_timeout: float = 60.0,
    ):
        self.driver = driver or UiaDriver()
        self._sleep = sleeper
        self.settle_seconds = settle_seconds
        self.launch_timeout = launch_timeout

    # -- observation -----------------------------------------------------

    def observe(self) -> GuardOutcome:
        """Classify the current app state without changing anything."""
        result = self.driver.app_state()
        if not result.ok:
            return GuardOutcome(UNAVAILABLE, str(result.get("reason_code", "app-state-failed")),
                                detail=str(result.get("detail", "")))

        state = dict(result.get("data", {}))
        if not state.get("running"):
            return GuardOutcome(UNAVAILABLE, "app-not-running", state=state,
                                remedy="Orbit can start it.")
        if not state.get("trusted_path"):
            # A process named ChatGPT from somewhere else is not our app, and is
            # certainly not something to launch a second copy alongside.
            return GuardOutcome(NEEDS_HUMAN_RESTART, "app-untrusted-path",
                                detail=str(state.get("executable", "")), state=state,
                                remedy="Verify which ChatGPT process is running before continuing.")
        if not state.get("windowed"):
            return GuardOutcome(NEEDS_HUMAN_RESTART, "app-has-no-window", state=state,
                                remedy="Open the ChatGPT window.")
        if state.get("accessibility_ready"):
            return GuardOutcome(READY, "ok", state=state)
        if not state.get("accessibility_flag"):
            return GuardOutcome(NEEDS_HUMAN_RESTART, "accessibility-flag-absent", state=state,
                                remedy=RESTART_INSTRUCTION)
        # Flag present but no semantic tree: usually the window is still coming
        # up. Distinguished from the flagless case because it may resolve on its
        # own, so `ensure` retries this one rather than reporting immediately.
        return GuardOutcome(NEEDS_HUMAN_RESTART, "accessibility-not-exposed", state=state,
                            remedy=RESTART_INSTRUCTION)

    # -- action ----------------------------------------------------------

    def ensure(self, *, allow_launch: bool = True) -> GuardOutcome:
        """Get to a usable surface, or explain precisely why that is impossible."""
        first = self.observe()
        if first.status == READY:
            return first

        if first.reason_code == "accessibility-not-exposed":
            settled = self._settle()
            if settled is not None:
                return settled
            return first

        if first.reason_code != "app-not-running":
            return first          # running and broken: never ours to restart

        if not allow_launch:
            return GuardOutcome(UNAVAILABLE, "launch-not-permitted", state=first.state,
                                remedy="Start ChatGPT Desktop, or re-run with launch allowed.")

        launched = self.driver.launch_app(self.launch_timeout)
        if not launched.ok:
            return GuardOutcome(UNAVAILABLE, str(launched.get("reason_code", "launch-failed")),
                                detail=str(launched.get("detail", "")),
                                remedy="Start ChatGPT Desktop manually with --force-renderer-accessibility.")

        settled = self._settle()
        if settled is not None:
            return GuardOutcome(LAUNCHED, "ok", state=settled.state) if settled.status == READY else settled
        return GuardOutcome(NEEDS_HUMAN_RESTART, "accessibility-not-exposed-after-launch",
                            remedy=RESTART_INSTRUCTION)

    def _settle(self) -> Optional[GuardOutcome]:
        """Re-observe a few times; a freshly shown window needs a moment."""
        for _ in range(3):
            self._sleep(self.settle_seconds)
            again = self.observe()
            if again.status == READY:
                return again
            if again.reason_code not in ("accessibility-not-exposed", "app-not-running",
                                         "app-has-no-window"):
                return again
        return None
