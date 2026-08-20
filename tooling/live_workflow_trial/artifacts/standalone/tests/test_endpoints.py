"""The committed Orbit endpoint configuration.

This config is the only thing standing between "PM named a role" and "Orbit typed
into a conversation", so it is tested as a contract rather than as data: every
entry must be well formed, scoped consistently, and unambiguous, and the ones
marked disabled must genuinely refuse.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from standalone.bridge.contracts import BridgeError
from standalone.bridge.registry import KNOWN_ROLE_SLUGS, ChatEndpointRegistry, fold_title

CONFIG_PATH = Path(__file__).resolve().parents[1] / "bridge" / "orbit_endpoints.json"

# Verified live against the running desktop app during M0-WF-CLAUDE-AUTONOMOUS-LONGRUN-001.
OBSERVED_TITLES = [
    "Orbit PM",
    "Product Research",
    "Windows Workflow",
    "Architecture TL",
    "QA TL",
    "Android Worker",
    "Memory Worker",
]

ENABLED = ("orbit-pm", "windows-worker", "architecture-tl", "qa-safety", "product-research")
DISABLED = ("android-worker", "memory-worker")


class ConfigShapeTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.entries = self.raw["endpoints"]

    def test_EP_001_config_is_committed_and_parses(self):
        self.assertTrue(CONFIG_PATH.exists())
        self.assertTrue(self.raw["schema_version"].startswith("orbit.chat-endpoint-registry/"))

    def test_EP_002_every_directive_addressable_role_is_present(self):
        for slug in ENABLED:
            self.assertIn(slug, self.entries, f"{slug} missing from committed config")

    def test_EP_003_slugs_are_known_roles(self):
        for slug in self.entries:
            self.assertIn(slug, KNOWN_ROLE_SLUGS, f"{slug} is not a known role slug")

    def test_EP_004_entry_id_matches_its_key(self):
        for slug, entry in self.entries.items():
            self.assertEqual(entry["endpoint_id"], slug)

    def test_EP_005_scopes_are_uniform(self):
        for slug, entry in self.entries.items():
            self.assertEqual(entry["project_scope"], self.raw["project_scope"], slug)
            self.assertEqual(entry["workflow_scope"], self.raw["workflow_scope"], slug)

    def test_EP_006_titles_are_pairwise_unambiguous(self):
        folded = [fold_title(e["display_title"]) for e in self.entries.values()]
        self.assertEqual(len(folded), len(set(folded)), "two endpoints fold to the same title")

    def test_EP_007_config_carries_no_credentials(self):
        blob = CONFIG_PATH.read_text(encoding="utf-8").lower()
        for forbidden in ("token", "cookie", "password", "session_id", "bearer", "authorization"):
            self.assertNotIn(forbidden, blob, f"config mentions {forbidden}")

    def test_EP_008_conversation_identity_is_scoped_to_the_chat_list(self):
        prefix = self.raw["chat_list_name"] + "/"
        for slug, entry in self.entries.items():
            self.assertTrue(entry["conversation_identity"].startswith(prefix), slug)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.registry = ChatEndpointRegistry.from_orbit_config()
        self.raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.scopes = dict(project_scope=self.raw["project_scope"],
                           workflow_scope=self.raw["workflow_scope"])

    def resolve(self, slug, titles=OBSERVED_TITLES):
        return self.registry.resolve(slug, observed_titles=titles, **self.scopes)

    def test_EP_010_enabled_endpoints_resolve_against_observed_titles(self):
        for slug in ENABLED:
            self.assertEqual(self.resolve(slug).endpoint_id, slug)

    def test_EP_011_disabled_endpoints_refuse_even_though_the_chat_exists(self):
        for slug in DISABLED:
            with self.assertRaises(BridgeError) as ctx:
                self.resolve(slug)
            self.assertEqual(str(ctx.exception), "endpoint-disabled")

    def test_EP_012_unregistered_role_never_resolves(self):
        with self.assertRaises(BridgeError) as ctx:
            self.resolve("some-chat-a-handoff-mentioned")
        self.assertEqual(str(ctx.exception), "endpoint-not-registered")

    def test_EP_013_a_renamed_chat_is_a_miss_not_a_guess(self):
        titles = [t for t in OBSERVED_TITLES if t != "Windows Workflow"] + ["Windows Workflow v2"]
        with self.assertRaises(BridgeError) as ctx:
            self.resolve("windows-worker", titles)
        self.assertEqual(str(ctx.exception), "endpoint-not-observed")

    def test_EP_014_duplicate_chat_titles_refuse_rather_than_pick(self):
        with self.assertRaises(BridgeError) as ctx:
            self.resolve("orbit-pm", OBSERVED_TITLES + ["orbit pm"])
        self.assertEqual(str(ctx.exception), "endpoint-ambiguous-observed")

    def test_EP_015_wrong_workflow_scope_refuses(self):
        with self.assertRaises(BridgeError) as ctx:
            self.registry.resolve("orbit-pm", project_scope=self.raw["project_scope"],
                                  workflow_scope="some-other-trial",
                                  observed_titles=OBSERVED_TITLES)
        self.assertEqual(str(ctx.exception), "endpoint-workflow-scope-mismatch")

    def test_EP_016_wrong_project_scope_refuses(self):
        with self.assertRaises(BridgeError) as ctx:
            self.registry.resolve("orbit-pm", project_scope="NotOrbit",
                                  workflow_scope=self.raw["workflow_scope"],
                                  observed_titles=OBSERVED_TITLES)
        self.assertEqual(str(ctx.exception), "endpoint-project-scope-mismatch")


class LoaderTests(unittest.TestCase):
    def test_EP_020_loader_ids_match_the_config_keys(self):
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(ChatEndpointRegistry.from_orbit_config().ids(), sorted(raw["endpoints"]))

    def test_EP_021_malformed_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(BridgeError) as ctx:
                ChatEndpointRegistry.from_orbit_config(bad)
            self.assertEqual(str(ctx.exception), "endpoint-registry-malformed")

    def test_EP_022_half_specified_entry_is_rejected_at_load(self):
        """A partial entry is a config mistake, so the whole load fails loudly."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "endpoints.json"
            path.write_text(json.dumps({"endpoints": {"orbit-pm": {"display_title": "Orbit PM"}}}),
                            encoding="utf-8")
            with self.assertRaises(BridgeError) as ctx:
                ChatEndpointRegistry.from_orbit_config(path)
            self.assertTrue(str(ctx.exception).startswith("endpoint-missing-field:"))

    def test_EP_023_complete_entry_defaults_to_disabled_without_the_flag(self):
        """An operator who forgets `enabled` gets a dead endpoint, not a live one."""
        entry = dict(json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["endpoints"]["orbit-pm"])
        entry.pop("enabled")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "endpoints.json"
            path.write_text(json.dumps({"endpoints": {"orbit-pm": entry}}), encoding="utf-8")
            self.assertFalse(ChatEndpointRegistry.from_orbit_config(path).get("orbit-pm").enabled)


class CliTests(unittest.TestCase):
    def test_EP_030_cli_builds_a_loop_from_the_committed_config(self):
        from standalone.bridge import apprentice_cli

        with tempfile.TemporaryDirectory() as tmp:
            loop = apprentice_cli.build_loop(Path(tmp) / "state", "M0-WF-CLI-TEST")
            self.assertEqual(loop.work_item, "M0-WF-CLI-TEST")
            self.assertEqual(loop.adapter.registry.ids(),
                             ChatEndpointRegistry.from_orbit_config().ids())
            self.assertTrue((Path(tmp) / "state").exists())

    def test_EP_031_cli_exposes_every_loop_step(self):
        from standalone.bridge import apprentice_cli

        for verb in ("status", "wake", "poll", "dispatch", "await", "collect", "clear"):
            with self.assertRaises(SystemExit):
                apprentice_cli.main(["--state-dir", ".", "--work-item", "W", verb, "--help"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
