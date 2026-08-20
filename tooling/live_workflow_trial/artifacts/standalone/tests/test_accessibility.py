"""The accessibility runtime guard.

The property that matters most here is a negative one: there is no path through
this class that ends with a running ChatGPT process being closed. Several tests
below exist only to assert that absence.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

from standalone.bridge.accessibility import (
    LAUNCHED,
    NEEDS_HUMAN_RESTART,
    READY,
    UNAVAILABLE,
    AccessibilityGuard,
)
from standalone.bridge.uia import DRIVER_OPERATIONS, UiaResult


def ok(data: Dict[str, Any]) -> UiaResult:
    return UiaResult({"ok": True, "reason_code": "ok", "data": data})


def deny(reason: str, detail: str = "") -> UiaResult:
    return UiaResult({"ok": False, "reason_code": reason, "detail": detail})


RUNNING_READY = {
    "running": True, "windowed": True, "windowed_count": 1, "instance_ambiguous": False,
    "trusted_path": True, "accessibility_flag": True, "accessibility_ready": True,
    "web_content_present": True, "session_locked": False,
    "executable": r"C:\...\OpenAI.Codex_x\app\ChatGPT.exe", "descendants": 900,
}
NOT_RUNNING = {
    "running": False, "windowed": False, "windowed_count": 0, "instance_ambiguous": False,
    "trusted_path": False, "accessibility_flag": False, "accessibility_ready": False,
    "web_content_present": False, "session_locked": False, "executable": "",
}


def variant(**overrides) -> Dict[str, Any]:
    state = dict(RUNNING_READY)
    state.update(overrides)
    return state


class FakeDriver:
    """Records every operation so tests can assert what was never called."""

    def __init__(self, states: List[Dict[str, Any]], launch: UiaResult = None):
        self.states = list(states)
        self.launch_result = launch if launch is not None else ok({"launched": True})
        self.calls: List[str] = []

    def app_state(self) -> UiaResult:
        self.calls.append("app_state")
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return state if isinstance(state, UiaResult) else ok(state)

    def launch_app(self, timeout_seconds: float = 60.0) -> UiaResult:
        self.calls.append("launch_app")
        if not self.states or self.states[0].get("running"):
            pass
        return self.launch_result


def guard(driver) -> AccessibilityGuard:
    return AccessibilityGuard(driver, sleeper=lambda _s: None, settle_seconds=0.0)


class SurfaceTests(unittest.TestCase):
    def test_ACC_001_driver_exposes_both_guard_operations(self):
        self.assertIn("app_state", DRIVER_OPERATIONS)
        self.assertIn("launch_app", DRIVER_OPERATIONS)

    def test_ACC_002_guard_has_no_termination_capability(self):
        """The class must not even offer a way to end the app."""
        for forbidden in ("kill", "terminate", "close_app", "stop_app", "restart"):
            self.assertFalse(hasattr(AccessibilityGuard, forbidden), forbidden)

    def test_ACC_003_no_termination_operation_exists_in_the_driver(self):
        for op in DRIVER_OPERATIONS:
            self.assertNotIn("kill", op)
            self.assertNotIn("terminate", op)


class ObserveTests(unittest.TestCase):
    def test_ACC_010_usable_surface_is_ready(self):
        out = guard(FakeDriver([RUNNING_READY])).observe()
        self.assertEqual(out.status, READY)
        self.assertTrue(out.ok)

    def test_ACC_011_absent_app_is_unavailable_not_broken(self):
        out = guard(FakeDriver([NOT_RUNNING])).observe()
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "app-not-running")

    def test_ACC_012_missing_flag_needs_a_human_restart(self):
        out = guard(FakeDriver([variant(accessibility_flag=False, accessibility_ready=False)])).observe()
        self.assertEqual(out.status, NEEDS_HUMAN_RESTART)
        self.assertEqual(out.reason_code, "accessibility-flag-absent")
        self.assertIn("--force-renderer-accessibility", out.remedy)

    def test_ACC_013_untrusted_process_is_never_treated_as_our_app(self):
        out = guard(FakeDriver([variant(trusted_path=False, executable=r"C:\tmp\ChatGPT.exe")])).observe()
        self.assertEqual(out.status, NEEDS_HUMAN_RESTART)
        self.assertEqual(out.reason_code, "app-untrusted-path")

    def test_ACC_014_windowless_flagged_process_is_waited_for_not_restarted(self):
        """The immutable requirement is already met; a window may appear."""
        out = guard(FakeDriver([variant(windowed=False, windowed_count=0,
                                        accessibility_ready=False)])).observe()
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "window-not-ready")
        self.assertNotIn("--force-renderer-accessibility", out.remedy)

    def test_ACC_017_a_locked_session_is_not_a_restart_problem(self):
        """Restarting the app cannot unlock Windows, so it must not be advised."""
        out = guard(FakeDriver([variant(session_locked=True, accessibility_ready=False,
                                        web_content_present=False)])).observe()
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "interactive-session-unavailable")
        self.assertIn("Unlock", out.remedy)
        self.assertNotIn("--force-renderer-accessibility", out.remedy)

    def test_ACC_018_a_locked_session_outranks_the_hidden_tree_it_causes(self):
        """The tree is unreadable *because* of the lock; report the cause."""
        out = guard(FakeDriver([variant(session_locked=True, accessibility_ready=False,
                                        web_content_present=False, descendants=0)])).observe()
        self.assertEqual(out.reason_code, "interactive-session-unavailable")

    def test_ACC_019_a_non_chat_view_is_not_an_accessibility_failure(self):
        """Sign-in, settings or a modal: the renderer is fine, the view is not a chat."""
        out = guard(FakeDriver([variant(accessibility_ready=False,
                                        web_content_present=True)])).observe()
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "composer-not-present")
        self.assertNotIn("Close ChatGPT", out.remedy)

    def test_ACC_01A_an_opaque_renderer_still_needs_a_restart(self):
        """No composer *and* no web content at all: accessibility really is dead."""
        out = guard(FakeDriver([variant(accessibility_ready=False,
                                        web_content_present=False)])).observe()
        self.assertEqual(out.status, NEEDS_HUMAN_RESTART)
        self.assertEqual(out.reason_code, "accessibility-not-exposed")

    def test_ACC_01B_no_window_and_no_flag_reports_the_decisive_cause(self):
        """The flag cannot be acquired in place, so the window is not the problem."""
        out = guard(FakeDriver([variant(windowed=False, windowed_count=0,
                                        accessibility_flag=False,
                                        accessibility_ready=False)])).observe()
        self.assertEqual(out.status, NEEDS_HUMAN_RESTART)
        self.assertEqual(out.reason_code, "accessibility-flag-absent")

    def test_ACC_01C_two_windowed_instances_are_ambiguous_not_ready(self):
        """One instance's readiness must not authorise driving a different one."""
        out = guard(FakeDriver([variant(windowed_count=2, instance_ambiguous=True)])).observe()
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "multiple-instance-ambiguous")
        self.assertIn("2", out.detail)

    def test_ACC_01D_ambiguity_is_refused_even_when_a_composer_was_found(self):
        out = guard(FakeDriver([variant(windowed_count=3, instance_ambiguous=True,
                                        accessibility_ready=True)])).observe()
        self.assertNotEqual(out.status, READY)
        self.assertEqual(out.reason_code, "multiple-instance-ambiguous")

    def test_ACC_01E_electron_helper_processes_do_not_create_ambiguity(self):
        """Ten processes, one window: that is a normal Electron app, not two apps."""
        out = guard(FakeDriver([variant(process_count=10, windowed_count=1,
                                        instance_ambiguous=False)])).observe()
        self.assertEqual(out.status, READY)

    def test_ACC_015_driver_failure_is_unavailable(self):
        driver = FakeDriver([])
        driver.app_state = lambda: deny("driver-timeout")  # type: ignore[assignment]
        out = AccessibilityGuard(driver, sleeper=lambda _s: None).observe()
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "driver-timeout")

    def test_ACC_016_observe_changes_nothing(self):
        driver = FakeDriver([NOT_RUNNING])
        guard(driver).observe()
        self.assertEqual(driver.calls, ["app_state"])


class EnsureTests(unittest.TestCase):
    def test_ACC_020_ready_surface_needs_no_action(self):
        driver = FakeDriver([RUNNING_READY])
        out = guard(driver).ensure()
        self.assertEqual(out.status, READY)
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_021_absent_app_is_launched_then_confirmed(self):
        driver = FakeDriver([NOT_RUNNING, RUNNING_READY])
        out = guard(driver).ensure()
        self.assertEqual(out.status, LAUNCHED)
        self.assertTrue(out.ok)
        self.assertIn("launch_app", driver.calls)

    def test_ACC_022_running_without_the_flag_is_never_relaunched(self):
        """The whole point of the guard: report, do not restart."""
        driver = FakeDriver([variant(accessibility_flag=False, accessibility_ready=False)])
        out = guard(driver).ensure()
        self.assertEqual(out.status, NEEDS_HUMAN_RESTART)
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_023_untrusted_running_process_is_never_launched_alongside(self):
        driver = FakeDriver([variant(trusted_path=False)])
        guard(driver).ensure()
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_024_launch_can_be_withheld(self):
        driver = FakeDriver([NOT_RUNNING])
        out = guard(driver).ensure(allow_launch=False)
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "launch-not-permitted")
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_025_refused_launch_is_surfaced_verbatim(self):
        driver = FakeDriver([NOT_RUNNING], launch=deny("launch-refused-already-running", "1 process(es)"))
        out = guard(driver).ensure()
        self.assertEqual(out.status, UNAVAILABLE)
        self.assertEqual(out.reason_code, "launch-refused-already-running")

    def test_ACC_026_missing_package_is_unavailable_with_a_manual_remedy(self):
        driver = FakeDriver([NOT_RUNNING], launch=deny("launch-package-not-installed"))
        out = guard(driver).ensure()
        self.assertEqual(out.reason_code, "launch-package-not-installed")
        self.assertIn("manually", out.remedy)

    def test_ACC_027_launch_that_never_becomes_usable_needs_a_human(self):
        driver = FakeDriver([NOT_RUNNING, variant(accessibility_flag=True,
                                                  accessibility_ready=False,
                                                  web_content_present=False)])
        out = guard(driver).ensure()
        self.assertEqual(out.status, NEEDS_HUMAN_RESTART)
        self.assertFalse(out.ok)

    def test_ACC_02A_a_locked_session_is_never_settled_on(self):
        """Re-checking a lock only delays telling the human to unlock it."""
        driver = FakeDriver([variant(session_locked=True, accessibility_ready=False)])
        out = guard(driver).ensure()
        self.assertEqual(out.reason_code, "interactive-session-unavailable")
        self.assertEqual(driver.calls, ["app_state"])
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_02B_ambiguous_instances_never_trigger_a_launch(self):
        driver = FakeDriver([variant(windowed_count=2, instance_ambiguous=True)])
        guard(driver).ensure()
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_02C_a_view_that_becomes_a_chat_resolves_without_a_restart(self):
        driver = FakeDriver([variant(accessibility_ready=False, web_content_present=True),
                             RUNNING_READY])
        out = guard(driver).ensure()
        self.assertEqual(out.status, READY)
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_02D_a_view_that_stays_non_chat_is_reported_as_such(self):
        driver = FakeDriver([variant(accessibility_ready=False, web_content_present=True)])
        out = guard(driver).ensure()
        self.assertEqual(out.reason_code, "composer-not-present")
        self.assertFalse(out.ok)

    def test_ACC_028_a_slow_window_is_waited_for_not_failed(self):
        slow = variant(accessibility_ready=False)
        driver = FakeDriver([slow, slow, RUNNING_READY])
        out = guard(driver).ensure()
        self.assertEqual(out.status, READY)
        self.assertNotIn("launch_app", driver.calls)

    def test_ACC_029_a_hard_failure_during_settle_stops_retrying(self):
        driver = FakeDriver([variant(accessibility_ready=False), variant(trusted_path=False)])
        out = guard(driver).ensure()
        self.assertEqual(out.reason_code, "app-untrusted-path")


class DrivableTests(unittest.TestCase):
    """Preflight asks a different question than per-conversation readiness."""

    def test_ACC_040_a_non_chat_view_still_leaves_the_app_drivable(self):
        """Otherwise one stuck conversation hides every healthy one."""
        out = guard(FakeDriver([variant(accessibility_ready=False,
                                        web_content_present=True)])).observe()
        self.assertFalse(out.ok)
        self.assertTrue(out.drivable)

    def test_ACC_041_a_ready_surface_is_both(self):
        out = guard(FakeDriver([RUNNING_READY])).observe()
        self.assertTrue(out.ok)
        self.assertTrue(out.drivable)

    def test_ACC_042_genuinely_broken_states_are_not_drivable(self):
        for state in (variant(accessibility_flag=False, accessibility_ready=False,
                              web_content_present=False),
                      variant(session_locked=True),
                      variant(trusted_path=False),
                      variant(windowed_count=2, instance_ambiguous=True),
                      NOT_RUNNING):
            out = guard(FakeDriver([state])).observe()
            self.assertFalse(out.drivable, out.reason_code)

    def test_ACC_043_drivability_is_reported_alongside_readiness(self):
        payload = guard(FakeDriver([variant(accessibility_ready=False,
                                            web_content_present=True)])).observe().to_dict()
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["drivable"])


class OutcomeTests(unittest.TestCase):
    def test_ACC_030_outcome_serialises_for_the_status_file(self):
        payload = guard(FakeDriver([RUNNING_READY])).observe().to_dict()
        self.assertEqual(payload["status"], READY)
        self.assertTrue(payload["ok"])
        self.assertIn("state", payload)

    def test_ACC_031_outcome_carries_no_command_line_secrets(self):
        """State is reported to PM, so it must stay identity-only."""
        payload = guard(FakeDriver([RUNNING_READY])).observe().to_dict()
        self.assertNotIn("command_line", payload["state"])
        self.assertNotIn("cmdline", payload["state"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
