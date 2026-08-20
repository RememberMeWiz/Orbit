"""ChatGPT adapter coverage.

The driver is stubbed so these run offline and host-independently: they assert
the adapter's *decisions*, not the app's behaviour. Anything that needed the
real app was verified live and is recorded in the burst handoff instead.
"""
from __future__ import annotations

import inspect
import unittest
from pathlib import Path
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
        self.attached: List[str] = []
        self.attach_result = None
        self.transcript = ""
        self.send_enabled_after_stage = True
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
        return ok({"length": len(text), "method": "uia-value",
                   "send_enabled": self.send_enabled_after_stage})

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

    def attach_file(self, path):
        self.calls.append("attach_file")
        if self.attach_result is not None:
            return self.attach_result
        import os
        self.attached.append(os.path.basename(str(path)))
        return ok({"filename": os.path.basename(str(path)), "activation": "clipboard-file-drop"})

    def attachment_state(self):
        self.calls.append("attachment_state")
        return ok({"attached": list(self.attached), "count": len(self.attached)})

    def clear_attachments(self):
        self.calls.append("clear_attachments")
        removed = list(self.attached)
        self.attached = []
        return ok({"removed": removed, "remaining": []})

    def read_transcript_tail(self, max_chars=6000):
        self.calls.append("read_transcript_tail")
        return ok({"text": self.transcript, "nodes": 1, "total_length": len(self.transcript)})

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

    def test_A11Y_006_a_streaming_window_is_ready_not_broken(self):
        """Send is replaced by Stop mid-response; the window is still usable."""
        driver = StubDriver()
        driver.surface = {**driver.surface, "send_present": False, "stop_present": True}
        result = build(driver).surface_ready()
        self.assertTrue(result.ok, result.reason_code)

    def test_A11Y_007_no_transport_control_at_all_denies(self):
        driver = StubDriver()
        driver.surface = {**driver.surface, "send_present": False, "stop_present": False}
        result = build(driver).surface_ready()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "semantic-surface-incomplete")
        self.assertIn("transport", result.detail)

    def test_A11Y_008_readiness_does_not_imply_it_is_safe_to_send(self):
        """The two questions stay separate: send re-checks streaming itself."""
        driver = StubDriver()
        driver.surface = {**driver.surface, "send_present": False, "stop_present": True}
        driver.state_sequence = ["streaming"]
        adapter = build(driver)
        self.assertTrue(adapter.surface_ready().ok)
        adapter.composer_text = "x"
        result = adapter.send(expect_endpoint_id="orbit-pm")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "response-in-progress")

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

    def test_CHAT_002_a_stuck_chat_does_not_block_switching_away_from_it(self):
        """The chat currently shown may be behind a prompt; that is why we switch."""
        class Stuck(StubDriver):
            def __init__(self):
                super().__init__()
                # No composer until the switch happens.
                self.surface = {**self.surface, "composer_present": False,
                                "send_present": False, "attach_present": False}

            def focus_chat(self, *, chat_list_name, chat_title):
                result = super().focus_chat(chat_list_name=chat_list_name, chat_title=chat_title)
                self.surface = {**self.surface, "composer_present": True,
                                "send_present": True, "attach_present": True}
                return result

        result = build(Stuck()).focus("orbit-pm")
        self.assertTrue(result.ok, result.reason_code)
        self.assertEqual(result.data["active_chat_title"], "Orbit PM")

    def test_CHAT_002b_an_unreadable_chat_list_still_blocks_the_switch(self):
        driver = StubDriver()
        driver.surface = {**driver.surface, "chat_items": []}
        result = build(driver).focus("orbit-pm")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "chat-list-not-ready-in-time")
        self.assertNotIn("focus_chat:Orbit PM", driver.calls)

    def test_CHAT_002c_the_destination_must_still_have_a_composer(self):
        """Switching away from a stuck chat into another stuck chat is not success."""
        driver = StubDriver()
        driver.surface = {**driver.surface, "composer_present": False,
                          "send_present": False, "attach_present": False}
        result = build(driver).focus("orbit-pm")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "surface-not-ready-in-time")

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


class StagingMechanismTests(unittest.TestCase):
    """Staging must not depend on which window has focus."""

    def test_CHAT_009_staging_prefers_the_keystroke_free_path(self):
        driver = StubDriver()
        adapter = build(driver)
        adapter.stage_message("TOKEN body", verify_token="TOKEN")
        # No foreground-dependent path is involved at all.
        self.assertIn("set_message", driver.calls)

    def test_CHAT_009b_a_composer_the_app_did_not_register_blocks_the_send(self):
        """UIA can echo a value back that the editor model never took."""
        driver = StubDriver()
        driver.send_enabled_after_stage = False
        result = build(driver).stage_message("TOKEN body", verify_token="TOKEN")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "composer-not-registered-by-app")

    def test_CHAT_009c_an_older_driver_without_the_signal_still_works(self):
        """Absent field means unknown, which must not read as disabled."""
        class Older(StubDriver):
            def set_message(self, text):
                self.calls.append("set_message")
                self.composer_text = text
                return ok({"length": len(text), "method": "clipboard-paste"})

        result = build(Older()).stage_message("TOKEN body", verify_token="TOKEN")
        self.assertTrue(result.ok, result.reason_code)

    def test_CHAT_009d_driver_offers_a_separate_clear(self):
        """Clearing is its own intent, never expressible as sending nothing."""
        from standalone.bridge.uia import DRIVER_OPERATIONS
        self.assertIn("clear_composer", DRIVER_OPERATIONS)


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
        for forbidden in ("click_at", "type_keys", "run_script", "control_window",
                          "start_process", "kill_app", "terminate_app"):
            self.assertNotIn(forbidden, DRIVER_OPERATIONS)

    def test_UI_006b_the_only_launcher_is_the_fixed_accessibility_one(self):
        """`launch_app` was added deliberately in Phase C2 and is not a general
        process launcher: it names no executable, takes no arguments, refuses
        while anything is running, and has no counterpart that ends a process."""
        from standalone.bridge.uia import DRIVER_OPERATIONS, UiaDriver

        launchers = [op for op in DRIVER_OPERATIONS if "launch" in op or "start" in op]
        self.assertEqual(launchers, ["launch_app"])

        source = (Path(__file__).resolve().parents[1] / "bridge" / "uia_driver.ps1").read_text(encoding="utf-8")
        self.assertIn("launch-refused-already-running", source)
        self.assertIn("--force-renderer-accessibility", source)
        for verb in ("Stop-Process", "taskkill", "$_.Kill()", "CloseMainWindow"):
            self.assertNotIn(verb, source, f"driver must never end the app ({verb})")

        params = inspect.signature(UiaDriver.launch_app).parameters
        self.assertEqual(list(params), ["self", "timeout_seconds"])

    def test_UI_007_no_driver_operation_accepts_coordinates(self):
        from standalone.bridge.uia import DRIVER_OPERATIONS
        for op in DRIVER_OPERATIONS:
            self.assertNotIn("coord", op)
            self.assertNotIn("click", op)


class DriverParsingTests(unittest.TestCase):
    """Both of these were live failures, not hypotheticals."""

    def _driver(self, stdout: str):
        from types import SimpleNamespace
        from standalone.bridge.uia import UiaDriver

        def runner(argv, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

        return UiaDriver(runner=runner)

    def test_DRV_001_wrapped_json_is_parsed(self):
        """PowerShell wraps long output across lines; that is not a failure."""
        payload = '{"ok":true,"reason_code":"ok","data":{"text":"abc",' + chr(10) + '"nodes":3}}'
        result = self._driver(payload).call("snapshot", {})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["nodes"], 3)

    def test_DRV_002_utf8_is_requested_explicitly(self):
        """Conversation text is not ASCII; the locale codepage corrupts it."""
        self._driver('{"ok":true,"reason_code":"ok"}').call("snapshot", {})
        self.assertEqual(self.kwargs.get("encoding"), "utf-8")
        self.assertEqual(self.kwargs.get("errors"), "replace")

    def test_DRV_003_non_ascii_payload_survives(self):
        payload = '{"ok":true,"reason_code":"ok","data":{"text":"Write in Markdown… — café"}}'
        result = self._driver(payload).call("read_transcript_tail", {})
        self.assertTrue(result.ok)
        self.assertIn("caf", result.data["text"])

    def test_DRV_004_stdin_is_closed(self):
        """An interactive prompt would hang the driver forever."""
        import subprocess
        self._driver('{"ok":true,"reason_code":"ok"}').call("snapshot", {})
        self.assertEqual(self.kwargs.get("stdin"), subprocess.DEVNULL)

    def test_DRV_005_garbage_is_a_denial_not_an_exception(self):
        result = self._driver("not json at all").call("snapshot", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "driver-unparseable-output")


class HandoffHeaderFormatTests(unittest.TestCase):
    """Header presentation syntax, after the approved contract ruling.

    These originally pinned the *opposite* behaviour: a real backticked handoff
    was collected correctly and then rejected by the engine with
    work-item-metadata-mismatch, while every fixture passed because fixtures
    wrote values plain. That mismatch was reported rather than patched, because
    changing the canonical validator is a contract decision.

    It was subsequently approved with guards, so the canonical parser now
    unwraps exactly one full-value inline-code wrapper. The collector needs no
    special case of its own: it reads the same parser the engine reads, so the
    two cannot drift apart. Guard coverage lives in
    workflow/tests/test_header_scalars.py.
    """

    @staticmethod
    def _header(work: str, sender: str) -> str:
        return chr(10).join([
            "## Header",
            f"- Work Item: {work}",
            f"- From: {sender}",
            "- To: TL",
            "- Status: COMPLETE",
            "- Handoff ID: x",
            "- Sequence: 1",
            "",
        ])

    def test_HDR_001_backticked_header_now_matches_filename_group(self):
        from workflow.core.validation import parse_header
        parsed = parse_header(self._header("`M0-WF-LIVE-003`", "`WORKER`"))
        self.assertTrue(parsed.ok)
        self.assertEqual(parsed.fields["work item"], "M0-WF-LIVE-003")
        self.assertEqual(parsed.fields["from"], "WORKER")

    def test_HDR_002_plain_header_still_matches(self):
        from workflow.core.validation import parse_header
        parsed = parse_header(self._header("M0-WF-LIVE-003", "WORKER"))
        self.assertEqual(parsed.fields["work item"], "M0-WF-LIVE-003")

    def test_HDR_003_malformed_wrapper_is_not_repaired(self):
        """A mismatch must stay a mismatch; only presentation is normalised."""
        from workflow.core.validation import parse_header
        parsed = parse_header(self._header("`M0-WF-LIVE-003", "WORKER"))
        self.assertEqual(parsed.fields["work item"], "`M0-WF-LIVE-003")
        self.assertNotEqual(parsed.fields["work item"], "M0-WF-LIVE-003")


if __name__ == "__main__":
    unittest.main(verbosity=2)
