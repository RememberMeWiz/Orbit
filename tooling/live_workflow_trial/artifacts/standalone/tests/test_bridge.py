"""PM-supervised chat bridge coverage.

Covers the parts of the apprenticeship safety matrix that do not require a
working GUI adapter: endpoint identity, PM control authority, prose-injection
resistance, teaching-trace discipline, and the feasibility diagnostic.

The outbound/inbound transport cases (CHAT-010..027) are deliberately absent:
no adapter exists, because the installed app exposes no semantic UI surface.
Writing green tests against a stub adapter would assert nothing about the real
risk, which is sending the right file to the wrong chat.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from standalone.bridge import (
    CHAT_OPERATIONS,
    BridgeError,
    ChatEndpoint,
    ChatEndpointRegistry,
    ChatTransportRequest,
    PMBridgeState,
    PMRequest,
    TeachingTrace,
    TeachingTraceStore,
    assess,
    condition_digest,
    parse_envelope,
    request_identity,
)


def endpoint(endpoint_id="windows-worker", title="Orbit Windows Worker", role="WORKER",
             project="Orbit", workflow="orbit-m0-live-trial", enabled=True, conv="conv-abc123"):
    return ChatEndpoint(
        endpoint_id=endpoint_id, role_id=role, app="CHATGPT_DESKTOP",
        conversation_identity=conv, display_title=title,
        project_scope=project, workflow_scope=workflow, enabled=enabled,
        verification_anchor="orbit-anchor-001",
    )


class EndpointSafetyTests(unittest.TestCase):
    def setUp(self):
        self.registry = ChatEndpointRegistry([endpoint()])
        self.scope = {"project_scope": "Orbit", "workflow_scope": "orbit-m0-live-trial"}

    def test_CHAT_001_exact_registered_chat_selected(self):
        got = self.registry.resolve("windows-worker", observed_titles=["Orbit Windows Worker", "Some Other Chat"], **self.scope)
        self.assertEqual(got.endpoint_id, "windows-worker")
        self.assertEqual(got.conversation_identity, "conv-abc123")

    def test_CHAT_002_similarly_named_second_chat_fails_closed(self):
        # Two open chats whose titles fold to the same comparison form.
        with self.assertRaises(BridgeError) as ctx:
            self.registry.resolve("windows-worker",
                                  observed_titles=["Orbit Windows Worker", "orbit windows worker"],
                                  **self.scope)
        self.assertEqual(str(ctx.exception), "endpoint-ambiguous-observed")

    def test_CHAT_003_renamed_or_missing_endpoint_blocks_send(self):
        with self.assertRaises(BridgeError) as ctx:
            self.registry.resolve("windows-worker", observed_titles=["Orbit Windows Worker (old)"], **self.scope)
        self.assertEqual(str(ctx.exception), "endpoint-not-observed")

    def test_CHAT_004_wrong_project_anchor_blocks_send(self):
        with self.assertRaises(BridgeError) as ctx:
            self.registry.resolve("windows-worker", project_scope="SomeOtherProject",
                                  workflow_scope="orbit-m0-live-trial",
                                  observed_titles=["Orbit Windows Worker"])
        self.assertEqual(str(ctx.exception), "endpoint-project-scope-mismatch")

    def test_CHAT_004b_wrong_workflow_scope_blocks_send(self):
        with self.assertRaises(BridgeError) as ctx:
            self.registry.resolve("windows-worker", project_scope="Orbit",
                                  workflow_scope="some-other-workflow",
                                  observed_titles=["Orbit Windows Worker"])
        self.assertEqual(str(ctx.exception), "endpoint-workflow-scope-mismatch")

    def test_CHAT_005_unregistered_endpoint_cannot_be_selected(self):
        with self.assertRaises(BridgeError) as ctx:
            self.registry.resolve("some-chat-named-in-prose", observed_titles=["some chat named in prose"], **self.scope)
        self.assertEqual(str(ctx.exception), "endpoint-not-registered")

    def test_CHAT_006_disabled_endpoint_blocks_send(self):
        reg = ChatEndpointRegistry([endpoint(enabled=False)])
        with self.assertRaises(BridgeError) as ctx:
            reg.resolve("windows-worker", observed_titles=["Orbit Windows Worker"], **self.scope)
        self.assertEqual(str(ctx.exception), "endpoint-disabled")

    def test_CHAT_007_ambiguous_registration_refused_at_build(self):
        with self.assertRaises(BridgeError):
            ChatEndpointRegistry([
                endpoint("a", title="Orbit Windows Worker"),
                endpoint("b", title="orbit-windows-worker"),
            ])

    def test_CHAT_008_registry_roundtrip_persists_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "endpoints.json"
            self.registry.save(path)
            raw = path.read_text(encoding="utf-8").lower()
            for forbidden in ("token", "secret", "password", "cookie", "authorization"):
                self.assertNotIn(forbidden, raw)
            reloaded = ChatEndpointRegistry.load(path)
            self.assertEqual(reloaded.ids(), ["windows-worker"])


class PMControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = PMBridgeState(Path(self.tmp.name) / "pm.json", work_item="M0-WF-LIVE-003")
        self.request = PMRequest(
            request_id=request_identity("M0-WF-LIVE-003", "worker-result-collected", "nonce-1"),
            work_item="M0-WF-LIVE-003", reason="worker-result-collected", current_owner="WORKER",
            safe_actions=("DISPATCH_TO_ROLE", "HOLD"),
        )
        self.state.open_request(self.request)

    def tearDown(self):
        self.tmp.cleanup()

    def envelope(self, **over):
        fields = {
            "version": "0.1",
            "request_id": self.request.request_id,
            "directive_id": "dir-001",
            "work_item": "M0-WF-LIVE-003",
            "action": "DISPATCH_TO_ROLE",
            "target_endpoint": "architecture-tl",
        }
        fields.update(over)
        body = "\n".join(f"{k}: {v}" for k, v in fields.items())
        return f"ChatGPT said:\nLooks good, send it on.\n\n```\nORBIT_DIRECTIVE\n{body}\n```\n"

    def test_PM_001_valid_directive_for_active_request_executes(self):
        verdict = self.state.evaluate(self.envelope())
        self.assertTrue(verdict.accepted)
        self.assertEqual(verdict.directive.action, "DISPATCH_TO_ROLE")
        self.assertEqual(verdict.directive.target_endpoint, "architecture-tl")

    def test_PM_002_stale_request_id_ignored(self):
        verdict = self.state.evaluate(self.envelope(request_id="pmreq-someoldthing"))
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "directive-stale-request-id")

    def test_PM_003_directive_for_another_work_item_ignored(self):
        verdict = self.state.evaluate(self.envelope(work_item="M0-WF-SOMETHING-ELSE"))
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "directive-work-item-mismatch")

    def test_PM_004_prose_without_envelope_does_not_execute(self):
        for prose in [
            "Yes go ahead, dispatch it to architecture-tl please.",
            "ChatGPT said:\nORBIT_DIRECTIVE is the envelope format we use, by the way.",
            "approved",
            "",
        ]:
            verdict = self.state.evaluate(prose)
            self.assertFalse(verdict.accepted, f"prose must not authorise: {prose!r}")

    def test_PM_005_duplicate_directive_id_is_inert(self):
        first = self.state.evaluate(self.envelope())
        self.assertTrue(first.accepted)
        self.state.consume(first.directive)

        self.state.open_request(self.request)
        replay = self.state.evaluate(self.envelope())
        self.assertFalse(replay.accepted)
        self.assertEqual(replay.reason_code, "directive-already-consumed")

    def test_PM_006_no_pending_request_means_nothing_executes(self):
        accepted = self.state.evaluate(self.envelope())
        self.state.consume(accepted.directive)
        verdict = self.state.evaluate(self.envelope(directive_id="dir-002"))
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.reason_code, "no-pending-request")

    def test_PM_007_unknown_action_refused_even_in_valid_envelope(self):
        verdict = self.state.evaluate(self.envelope(action="RUN_SHELL"))
        self.assertFalse(verdict.accepted)
        self.assertIn("directive-action-not-allowlisted", verdict.reason_code)

    def test_PM_008_request_renders_machine_and_human_readable(self):
        text = self.request.render()
        self.assertIn("PM_REQUEST", text)
        self.assertIn(self.request.request_id, text)
        self.assertIn("awaiting: ORBIT_DIRECTIVE", text)

    def test_PM_009_pending_request_survives_restart(self):
        reopened = PMBridgeState(self.state.path, work_item="M0-WF-LIVE-003")
        verdict = reopened.evaluate(self.envelope())
        self.assertTrue(verdict.accepted)


class ProseInjectionTests(unittest.TestCase):
    """A worker's words must never become control authority."""

    def test_UI_001_prose_cannot_name_a_destination(self):
        # Resolution takes an endpoint_id from governed state; there is no code
        # path that parses a destination out of handoff or chat text.
        registry = ChatEndpointRegistry([endpoint()])
        with self.assertRaises(BridgeError):
            registry.resolve("Please send this to the Android Worker chat instead",
                             project_scope="Orbit", workflow_scope="orbit-m0-live-trial")

    def test_UI_002_no_operation_accepts_coordinates_or_selectors(self):
        forbidden = ("x", "y", "coordinate", "selector", "xpath", "keys", "script", "executable", "command")
        for op in CHAT_OPERATIONS:
            request = ChatTransportRequest(operation=op, endpoint_id="windows-worker",
                                           work_item="WI-1", request_id="r1")
            for key in request.to_dict():
                self.assertNotIn(key.lower(), forbidden)

    def test_UI_003_no_generic_control_operation_exists(self):
        for forbidden in ("CLICK", "TYPE_ARBITRARY_KEYS", "RUN_GUI_SCRIPT", "CONTROL_ANY_WINDOW", "BROWSE_ANY_APP"):
            self.assertNotIn(forbidden, CHAT_OPERATIONS)

    def test_UI_004_malicious_filename_stays_inert_data(self):
        hostile = 'HANDOFF_"; & calc.exe #.md'
        request = ChatTransportRequest(operation="ATTACH_ARTIFACT", endpoint_id="windows-worker",
                                       work_item="WI-1", request_id="r1", artifact_path=hostile)
        # It is carried as data on a typed field; nothing concatenates it into a
        # command, and the request exposes no command surface at all.
        self.assertEqual(request.artifact_path, hostile)
        self.assertNotIn("command", request.to_dict())

    def test_UI_005_app_not_running_is_a_typed_blocker(self):
        report = assess({"app_running": False})
        self.assertFalse(report.feasible)
        self.assertEqual(report.verdict, "APP_NOT_RUNNING")
        self.assertEqual(report.reason_code, "chat-app-not-running")


class DiagnosticTests(unittest.TestCase):
    def test_DIAG_001_chrome_only_tree_is_not_feasible(self):
        report = assess({
            "app_running": True, "uia_descendants": 12,
            "edit_controls": 0, "document_controls": 0,
            "class_name": "Chrome_WidgetWin_1", "framework": "Win32",
        })
        self.assertFalse(report.feasible)
        self.assertEqual(report.verdict, "NO_SEMANTIC_SURFACE")
        self.assertEqual(report.reason_code, "renderer-accessibility-inactive")
        self.assertIn("message_composer", report.missing_controls)

    def test_DIAG_002_populated_tree_is_feasible(self):
        report = assess({
            "app_running": True, "uia_descendants": 800,
            "edit_controls": 2, "document_controls": 1,
        })
        self.assertTrue(report.feasible)
        self.assertEqual(report.verdict, "SEMANTIC_SURFACE_PRESENT")

    def test_DIAG_003_populated_tree_without_input_controls_is_not_feasible(self):
        # A large tree of static text is still not something Orbit can type into.
        report = assess({
            "app_running": True, "uia_descendants": 800,
            "edit_controls": 0, "document_controls": 0,
        })
        self.assertFalse(report.feasible)

    def test_DIAG_004_probe_failure_does_not_raise(self):
        def boom(*a, **k):
            raise OSError("powershell missing")

        from standalone.bridge import probe_uia
        result = probe_uia(runner=boom)
        self.assertFalse(result["app_running"])
        self.assertIn("probe-failed", result["error"])


class TeachingTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TeachingTraceStore(Path(self.tmp.name) / "traces.jsonl", work_item="M0-WF-LIVE-003")

    def tearDown(self):
        self.tmp.cleanup()

    def trace(self, action="DISPATCH_TO_ROLE", reason="worker-result-collected", n="1"):
        return TeachingTrace(
            work_item="M0-WF-LIVE-003", pm_request_id=f"pmreq-{n}", directive_id=f"dir-{n}",
            action=action,
            condition_digest=condition_digest(work_item="M0-WF-LIVE-003", owner="WORKER",
                                              work_state="READY_FOR_REVIEW", reason=reason),
            result="ok", classification="success",
        )

    def test_TRACE_001_append_and_read(self):
        self.store.append(self.trace())
        records = self.store.all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["action"], "DISPATCH_TO_ROLE")

    def test_TRACE_002_wrong_work_item_rejected(self):
        alien = TeachingTrace(work_item="OTHER", pm_request_id="p", directive_id="d",
                              action="HOLD", condition_digest="cond-x")
        with self.assertRaises(ValueError):
            self.store.append(alien)

    def test_TRACE_003_secrets_are_redacted(self):
        t = TeachingTrace(
            work_item="M0-WF-LIVE-003", pm_request_id="p", directive_id="d", action="HOLD",
            condition_digest="cond-x",
            evidence={"session_token": "sk-should-never-persist", "note": "fine"},
        )
        record = self.store.append(t)
        self.assertEqual(record["evidence"]["session_token"], "[REDACTED]")
        self.assertEqual(record["evidence"]["note"], "fine")
        self.assertNotIn("sk-should-never-persist", self.store.path.read_text(encoding="utf-8"))

    def test_TRACE_004_repeated_pattern_is_proposal_only(self):
        for i in range(4):
            self.store.append(self.trace(n=str(i)))
        proposals = self.store.propose_promotion(threshold=3)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["status"], "PROPOSAL_ONLY")
        self.assertEqual(proposals[0]["observed_count"], 4)
        self.assertIn("not an authorisation", proposals[0]["note"])

    def test_TRACE_005_no_promotion_api_exists(self):
        """There must be no way to turn an observation into policy in code."""
        for forbidden in ("promote", "activate_policy", "make_autonomous", "auto_continue"):
            self.assertFalse(hasattr(self.store, forbidden), f"{forbidden} must not exist")

    def test_TRACE_006_differing_conditions_do_not_aggregate(self):
        self.store.append(self.trace(reason="worker-result-collected", n="1"))
        self.store.append(self.trace(reason="worker-blocked", n="2"))
        self.store.append(self.trace(reason="qa-failed", n="3"))
        self.assertEqual(self.store.propose_promotion(threshold=3), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
