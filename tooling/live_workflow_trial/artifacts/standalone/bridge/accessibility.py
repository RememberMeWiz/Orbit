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
        """The conversation currently on screen is a usable chat."""
        return self.status in (READY, LAUNCHED)

    @property
    def drivable(self) -> bool:
        """Orbit can reach and read this app, even if this view is not a chat.

        A separate question from `ok`, and the one a preflight should ask.
        Readiness is measured against whichever conversation happens to be on
        screen, so gating the whole app on it means one conversation stuck
        behind a confirmation prompt or a file preview makes every *other*
        conversation unreachable -- including the PM chat Orbit would use to
        report the problem. Focusing a specific endpoint is what decides
        whether that endpoint is usable, and it fails with its own reason.
        """
        return self.ok or self.reason_code in DRIVABLE_REASONS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "drivable": self.drivable,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "remedy": self.remedy,
            "state": dict(self.state),
        }


RESTART_INSTRUCTION = (
    "Close ChatGPT Desktop completely, then let Orbit start it, or start it "
    "manually with --force-renderer-accessibility."
)

# States that a moment of waiting can legitimately resolve, because the window
# is still being constructed or the view is still settling. Everything else is
# reported on the first observation: re-checking a locked session or a missing
# launch flag only delays telling the human what to do about it.
TRANSIENT_REASONS = frozenset({
    "accessibility-not-exposed",
    "window-not-ready",
    "composer-not-present",
    "non-chat-surface",
})

# Waiting for a just-launched app to appear is also transient, but only *after*
# a launch. Before one, "not running" is the signal to start it, not to wait.
_SETTLE_REASONS = TRANSIENT_REASONS | {"app-not-running"}

# States where the app itself is healthy and readable -- renderer exposed, one
# unambiguous instance, session unlocked -- and only the conversation on screen
# is unusable. Switching conversations resolves these, so they must not gate a
# preflight; see GuardOutcome.drivable.
# `non-chat-surface` is deliberately absent: with no conversation shell there is
# no list to select an endpoint from, so nothing downstream could succeed.
DRIVABLE_REASONS = frozenset({"composer-not-present"})


class AccessibilityGuard:
    def __init__(
        self,
        driver: Optional[UiaDriver] = None,
        *,
        chat_list_name: str = "",
        sleeper: Callable[[float], None] = time.sleep,
        settle_seconds: float = 2.0,
        launch_timeout: float = 60.0,
    ):
        self.driver = driver or UiaDriver()
        self.chat_list_name = chat_list_name
        self._sleep = sleeper
        self.settle_seconds = settle_seconds
        self.launch_timeout = launch_timeout

    # -- observation -----------------------------------------------------

    def observe(self) -> GuardOutcome:
        """Classify the current app state without changing anything.

        Order matters. The rule throughout is that a state gets reported with
        its *decisive* cause and the remedy that actually fixes it -- telling a
        human to restart something a restart cannot repair is worse than saying
        nothing, because they will do it.
        """
        result = self.driver.app_state(self.chat_list_name)
        if not result.ok:
            return GuardOutcome(UNAVAILABLE, str(result.get("reason_code", "app-state-failed")),
                                detail=str(result.get("detail", "")))

        state = dict(result.get("data", {}))
        if not state.get("running"):
            return GuardOutcome(UNAVAILABLE, "app-not-running", state=state,
                                remedy="Orbit can start it.")

        # Trust and instance count come from the process table, not the UIA
        # tree, so both are established whether or not the desktop is readable --
        # and they outrank the lock for that reason. Reporting only "locked"
        # would hide the stronger fact that Orbit had selected a process outside
        # the trusted package.
        if not state.get("path_observed", bool(state.get("executable"))):
            return GuardOutcome(UNAVAILABLE, "app-path-unreadable", state=state,
                                remedy="Check permissions on the ChatGPT process, then retry.")
        if not state.get("trusted_path"):
            # A trust mismatch is not evidence that restarting is the repair.
            # The right move might be finishing an update, closing an old
            # install, or investigating a foreign process -- the guard cannot
            # tell which, so it reports rather than prescribes.
            return GuardOutcome(UNAVAILABLE, "app-untrusted-path",
                                detail=str(state.get("executable", "")), state=state,
                                remedy="Verify which ChatGPT instance and install is running "
                                       "before continuing.")

        # More than one instance owns a window, so "the app" has no single state
        # to report. Selection is "first windowed process", which is not a stable
        # identity contract across observations -- and READY is only meaningful
        # if downstream work binds to the same target. Refused the same way
        # ambiguous endpoints are: ambiguity is an error, never a best guess.
        if state.get("instance_ambiguous"):
            return GuardOutcome(
                UNAVAILABLE, "multiple-instance-ambiguous",
                detail=f"{state.get('windowed_count')} windowed instances", state=state,
                remedy="Close the extra ChatGPT windows so one instance is unambiguous.")

        # Only now the tree-derived states, since a locked workstation hides the
        # tree entirely and every conclusion below would otherwise be drawn from
        # an observation that could not have succeeded.
        if state.get("session_locked"):
            return GuardOutcome(UNAVAILABLE, "interactive-session-unavailable", state=state,
                                remedy="Unlock the Windows session. Restarting the app will not help.")

        if not state.get("windowed"):
            # Narrow rule, not a general principle: prefer an observed cause over
            # a transient symptom only when that cause alone determines the
            # remedy and cannot resolve by waiting. A missing launch flag
            # qualifies -- it cannot be acquired in place, so a window appearing
            # later would change nothing.
            if not state.get("accessibility_flag"):
                return GuardOutcome(NEEDS_HUMAN_RESTART, "accessibility-flag-absent", state=state,
                                    remedy=RESTART_INSTRUCTION)
            return GuardOutcome(UNAVAILABLE, "window-not-ready", state=state,
                                remedy="Wait for the ChatGPT window, or open it from the taskbar.")

        if state.get("accessibility_ready"):
            return GuardOutcome(READY, "ok", state=state)

        # The one state where a relaunch really is the uniquely established
        # remedy, because the flag cannot be acquired in place.
        if not state.get("accessibility_flag"):
            return GuardOutcome(NEEDS_HUMAN_RESTART, "accessibility-flag-absent", state=state,
                                remedy=RESTART_INSTRUCTION)

        if state.get("renderer_semantics_present"):
            # Shell up, no composer: some conversation is behind a prompt, a file
            # preview or a modal. Switching conversations resolves it.
            if state.get("chat_surface_present"):
                return GuardOutcome(
                    UNAVAILABLE, "composer-not-present", state=state,
                    remedy="Open a conversation in ChatGPT, or dismiss whatever is covering "
                           "the composer — a file preview, a modal, or an in-conversation "
                           "confirmation prompt. No restart needed.")
            # Semantics but no conversation shell at all: sign-in, first run,
            # a full-screen settings or update surface.
            return GuardOutcome(
                UNAVAILABLE, "non-chat-surface", state=state,
                remedy="Sign in, or navigate back to the conversation list in the "
                       "running app. No restart needed.")

        # Flag present, window present, and after settling still no meaningful
        # semantic subtree. A relaunch may well fix it, but that is the human's
        # call: absence of controls is not positive evidence of a launch defect.
        return GuardOutcome(
            UNAVAILABLE, "accessibility-not-exposed", state=state,
            remedy="Restore a normal interactive ChatGPT surface. Relaunching with "
                   "--force-renderer-accessibility may help, but is not established "
                   "as the cause.")

    # -- action ----------------------------------------------------------

    def ensure(self, *, allow_launch: bool = True) -> GuardOutcome:
        """Get to a usable surface, or explain precisely why that is impossible."""
        first = self.observe()
        if first.status == READY:
            return first

        if first.reason_code in TRANSIENT_REASONS:
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
        """Re-observe a few times; a window mid-construction needs a moment.

        Returns the outcome once it stops being transient, or None if it never
        does -- in which case the caller reports the original, which is the
        honest answer: this state persisted.
        """
        for _ in range(3):
            self._sleep(self.settle_seconds)
            again = self.observe()
            if again.status == READY or again.reason_code not in _SETTLE_REASONS:
                return again
        return None
