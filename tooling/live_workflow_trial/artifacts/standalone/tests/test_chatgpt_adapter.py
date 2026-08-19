"""ChatGPT adapter coverage.

The driver is stubbed so these run offline and host-independently: they assert
the adapter's *decisions*, not the app's behaviour. Anything that needed the
real app was verified live and is recorded in the burst handoff instead.
"""
from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional

from standalone.bridge import ChatEndpoint, ChatEndpointRegistry, ChatGptAdapter
from standalone.bridge.uia import UiaResult

PROJECT = "Orbit"
WORKFLOW = "orbit-m0-live-trial"
CHAT_LIST = "Chats in Yong 2"


def ok(data: Optional[Dict[str, Any]] = None) -> UiaResult:
    return UiaResult({"ok": True, "reason_code": "ok", "data": data or {}})


def deny(reason: str) -> UiaResult:
    return UiaResult({"ok": False, "reason_code": reason, "detail": ""})


def endpoint(endpoint_id="orbit-pm", title="Orbit PM", role="PM"):
    return ChatEndpoint(
        endpoint_id=endpoint_id, role_id=role, app="CHATGPT_DESKTOP",
        conversation_identity=f"{CHAT_LIST}/{title}", display_title=title,
        project_scope=PROJECT, workflow_scope=WORKFLOW, enabled=True,
        verification_anchor=title,
    )


class StubDriver:
    """Scriptable stand-in for the PowerShell UIA driver."""

    def __init__(self, **overrides):
        self.calls: List[str] = []
        self.surface = {
            "composer_present": True, "send_present": True, "attach_present": True,
            "chat_items": ["Orbit PM", "Windows Workflow"], "descendants": 900,
        }
        self.header = "Orbit PM"
        self.composer_text = ""
        self.state_sequence: List[str] = ["idle"]
        self.state_index = 0
        self.send_result = ok({"sent": True})
        self.artifacts = {"saveable": [], "previewable": []}
        self.__dict__.update(overrides)

    def snapshot(self, chat_list_name=""):
        self.calls.append("snapshot")
        return ok(dict(self.surface))

    def focus_chat(self, *, chat_list_name, chat_title):
        self.calls.append(f"focus_chat:{chat_title}")
        self.header = chat_title
        return ok({"focused_title": chat_title})

    def active_chat(self):
        self.calls.append("active_chat")
        return ok({"active_chat_title": self.header, "project_markers": ["Project: Yong 2"]})

    def set_message(self, text):
        self.calls.append("set_message")
        self.composer_text = text
        return ok({"length": len(text), "method": "clipboard-paste"})

    def read_composer(self):
        self.calls.append("read_composer")
        return ok({"text": self.composer_text, "length": len(self.composer_text)})

    def response_state(self):
        self.calls.append("response_state")
        i = min(self.state_index, len(self.state_sequence) - 1)
        self.state_index += 1
        state = self.state_sequence[i]
        return ok({"state": state, "send_present": state != "streaming", "stop_present": state == "streaming"})

    def press_send(self):
        self.calls.append("press_send")
        return self.send_result

    def list_artifacts(self):
        self.calls.append("list_artifacts")
        return ok(dict(self.artifacts))

    def call(self, operation, params=None):
        return getattr(self, operation)()


def build(driver: StubDriver, endpoints=None) -> ChatGptAdapter:
    registry = ChatEndpointRegistry(endpoints or [endpoint(), endpoint("windows-workflow", "Windows Workflow", "WORKER")])
    return ChatGptAdapter(
        registry, driver=driver, project_scope=PROJECT, workflow_scope=WORKFLOW,
        chat_list_name=CHAT_LIST, sleeper=lambda _s: None, clock=_FakeClock(),
    )


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


class SurfaceTests(unittest.TestCase):
    def test_A11Y_001_semantic_surface_enables_adapter(self):
        adapter = build(StubDriver())
        self.assertTrue(adapter.surface_ready().ok)

    def test_A11Y_003_opaque_surface_denies_without_restarting_anything(self):
        driver = StubDriver()
        driver.surface = {**driver.surface, "composer_present": False}
        adapter = build(driver)
        result = adapter.await_surface(timeout=3.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "surface-not-ready-in-time")
        # No restart, kill, or launch is attempted anywhere in the adapter.
        self.assertNotIn("launch", " ".join(driver.calls))

    def test_A11Y_004_surface_loss_mid_wait_halts_safely(self):
        class Dropping(StubDriver):
            def response_state(self):
                self.calls.append("response_state")
                return deny("chat-window-unavailable")

        adapter = build(Dropping())
        obs = adapter.wait_for_response(timeout=30.0)
        self.assertEqual(obs.state, "error")
        self.assertEqual(obs.detail, "chat-window-unavailable")

    def test_A11Y_005_app_absent_denies(self):
        class Absent(StubDriver):
            def snapshot(self, chat_list_name=""):
                return deny("chat-app-not-running")

        result = build(Absent()).surface_ready()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat-app-not-running")


class FocusTests(unittest.TestCase):
    def test_CHAT_001_focus_verified_by_header(self):
        adapter = build(StubDriver())
        result = adapter.focus("orbit-pm")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["active_chat_title"], "Orbit PM")

    def test_CHAT_003_header_disagreement_fails_closed(self):
        class Wrong(StubDriver):
            def focus_chat(self, *, chat_list_name, chat_title):
                self.calls.append("focus_chat")
                self.header = "Some Other Chat"   # click landed elsewhere
                return ok({"focused_title": chat_title})

        result = build(Wrong()).focus("orbit-pm")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "focus-verification-failed")

    def test_CHAT_005_unregistered_endpoint_denied(self):
        result = build(StubDriver()).focus("some-chat-from-prose")
        self.assertFalse(result.ok)
        self.assertIn("endpoint-not-registered", result.reason_code)


class SendTests(unittest.TestCase):
    def test_CHAT_011_unverified_staged_message_blocks_send(self):
        class Swallowing(StubDriver):
            def set_message(self, text):
                self.calls.append("set_message")
                self.composer_text = ""   # paste silently lost
                return ok({"length": 0})

        driver = Swallowing()
        adapter = build(driver)
        result = adapter.stage_message("hello REQ-123", verify_token="REQ-123")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "staged-message-verification-failed")
        self.assertNotIn("press_send", driver.calls)

    def test_CHAT_011b_verified_staged_message_allows_send(self):
        adapter = build(StubDriver())
        result = adapter.stage_message("payload REQ-123 here", verify_token="REQ-123")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["verified_token"], "REQ-123")

    def test_CHAT_012_destination_change_between_focus_and_send_refused(self):
        driver = StubDriver()
        adapter = build(driver)
        self.assertTrue(adapter.focus("orbit-pm").ok)
        driver.header = "Windows Workflow"   # user switched chat underneath
        result = adapter.send(expect_endpoint_id="orbit-pm")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "destination-changed-before-send")
        self.assertEqual(result.delivery_state, "FAILED")
        self.assertNotIn("press_send", driver.calls)

    def test_CHAT_015_streaming_blocks_send(self):
        driver = StubDriver(state_sequence=["streaming"])
        adapter = build(driver)
        result = adapter.send(expect_endpoint_id="orbit-pm")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "response-in-progress")
        self.assertNotIn("press_send", driver.calls)

    def test_CHAT_015b_streaming_blocks_staging(self):
        adapter = build(StubDriver(state_sequence=["streaming"]))
        result = adapter.stage_message("x", verify_token="")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "response-in-progress")

    def test_CHAT_014_send_reports_unconfirmed_not_delivered(self):
        """A pressed Send is not proof of delivery."""
        adapter = build(StubDriver())
        result = adapter.send(expect_endpoint_id="orbit-pm")
        self.assertTrue(result.ok)
        self.assertEqual(result.delivery_state, "SENT_UNCONFIRMED")
        self.assertNotEqual(result.delivery_state, "DELIVERED")

    def test_CHAT_016_driver_send_failure_is_failed_not_ambiguous(self):
        driver = StubDriver(send_result=deny("send-control-disabled"))
        result = build(driver).send(expect_endpoint_id="orbit-pm")
        self.assertFalse(result.ok)
        self.assertEqual(result.delivery_state, "FAILED")


class ResponseTests(unittest.TestCase):
    def test_CHAT_017_streaming_is_not_complete(self):
        adapter = build(StubDriver(state_sequence=["streaming", "streaming", "streaming"]))
        obs = adapter.wait_for_response(timeout=5.0)
        self.assertEqual(obs.state, "timeout")

    def test_CHAT_018_completion_requires_sustained_idle_after_streaming(self):
        adapter = build(StubDriver(state_sequence=["streaming", "streaming", "idle", "idle", "idle", "idle", "idle", "idle", "idle", "idle", "idle"]))
        obs = adapter.wait_for_response(timeout=200.0)
        self.assertEqual(obs.state, "complete")

    def test_CHAT_018b_idle_without_prior_streaming_is_not_completion(self):
        """Idle before the reply even starts must not be read as a finished reply."""
        adapter = build(StubDriver(state_sequence=["idle"] * 20))
        obs = adapter.wait_for_response(timeout=12.0)
        self.assertEqual(obs.state, "timeout")


class ArtifactTests(unittest.TestCase):
    def test_CHAT_020_single_expected_artifact_found(self):
        driver = StubDriver(artifacts={"saveable": ["HANDOFF_X.md", "other.md"], "previewable": []})
        result = build(driver).find_expected_artifact("HANDOFF_X.md")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["filename"], "HANDOFF_X.md")

    def test_CHAT_021_absent_artifact_reports_not_present(self):
        driver = StubDriver(artifacts={"saveable": ["unrelated.md"], "previewable": []})
        result = build(driver).find_expected_artifact("HANDOFF_X.md")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-not-present")

    def test_CHAT_022_duplicate_artifacts_fail_closed(self):
        driver = StubDriver(artifacts={"saveable": ["HANDOFF_X.md", "HANDOFF_X.md"], "previewable": []})
        result = build(driver).find_expected_artifact("HANDOFF_X.md")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-ambiguous")

    def test_CHAT_027_neighbouring_files_are_never_substituted(self):
        driver = StubDriver(artifacts={"saveable": ["HANDOFF_Y.md", "notes.txt"], "previewable": []})
        result = build(driver).find_expected_artifact("HANDOFF_X.md")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-not-present")


class DriverSurfaceTests(unittest.TestCase):
    def test_UI_006_driver_operations_are_allowlisted(self):
        from standalone.bridge.uia import DRIVER_OPERATIONS, UiaDriver
        result = UiaDriver().call("evil_operation", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "driver-operation-not-allowlisted")
        for forbidden in ("click_at", "type_keys", "run_script", "control_window", "launch_app"):
            self.assertNotIn(forbidden, DRIVER_OPERATIONS)

    def test_UI_007_no_driver_operation_accepts_coordinates(self):
        from standalone.bridge.uia import DRIVER_OPERATIONS
        for op in DRIVER_OPERATIONS:
            self.assertNotIn("coord", op)
            self.assertNotIn("click", op)


if __name__ == "__main__":
    unittest.main(verbosity=2)
