"""Canonical header scalar normalisation.

Real Orbit handoffs write header values wrapped in Markdown inline code
(``- Work Item: `M0-...` ``) while the original fixtures wrote them plain. Both
denote the same value, so the canonical parser unwraps exactly one full-value
wrapper.

The point of this module is the guards, not the happy path. A malformed header
must never be silently repaired into a *different* value, and nothing about
identity -- filename, digest, handoff id, sequence, sender, recipient -- may
shift because of presentation syntax.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from workflow.core.bootstrap import bootstrap_workspace
from workflow.core.engine import WorkflowEngine
from workflow.core.manifest import load_manifest
from workflow.core.runtime import resolve_runtime_paths
from workflow.core.validation import parse_header, unwrap_scalar
from windows.adapters.place_packet import PlacePacketExecutor

PACKAGE_ROOT = Path(__file__).resolve().parents[3]

BACKTICKED = """# Orbit Handoff

## Header
- Work Item: `{work}`
- From: `{sender}`
- To: `{recipient}`
- Status: `{status}`
- Handoff ID: `{hid}`
- Sequence: `{seq}`

## Executive Summary
Backticked header fixture.
"""

PLAIN = """# Orbit Handoff

## Header
- Work Item: {work}
- From: {sender}
- To: {recipient}
- Status: {status}
- Handoff ID: {hid}
- Sequence: {seq}

## Executive Summary
Plain header fixture.
"""


def fields(template: str, **kw) -> dict:
    defaults = dict(work="M0-WF-LIVE-003", sender="WORKER", recipient="TL",
                    status="COMPLETE", hid="h-1", seq="1")
    defaults.update(kw)
    parsed = parse_header(template.format(**defaults))
    assert parsed.ok, parsed.reason
    return parsed.fields


class UnwrapGuardTests(unittest.TestCase):
    def test_HDRS_001_single_full_wrapper_is_unwrapped(self):
        self.assertEqual(unwrap_scalar("`M0-WF-LIVE-003`"), "M0-WF-LIVE-003")

    def test_HDRS_002_plain_value_unchanged(self):
        self.assertEqual(unwrap_scalar("M0-WF-LIVE-003"), "M0-WF-LIVE-003")

    def test_HDRS_003_unbalanced_is_not_repaired(self):
        for raw in ("`M0-WF-LIVE-003", "M0-WF-LIVE-003`", "``M0-WF-LIVE-003`"):
            self.assertEqual(unwrap_scalar(raw), raw, raw)

    def test_HDRS_004_nested_or_doubled_is_not_repaired(self):
        for raw in ("``M0``", "```M0```"):
            self.assertEqual(unwrap_scalar(raw), raw, raw)

    def test_HDRS_005_multiple_or_interior_backticks_untouched(self):
        for raw in ("`a`b`c`", "pre `M0` post", "`a` `b`"):
            self.assertEqual(unwrap_scalar(raw), raw, raw)

    def test_HDRS_006_empty_wrapper_is_not_collapsed(self):
        # "``" must not become the empty string; an empty critical field is a
        # rejection, and silently producing one would hide a malformed header.
        self.assertEqual(unwrap_scalar("``"), "``")

    def test_HDRS_007_backticked_and_plain_headers_agree(self):
        self.assertEqual(fields(BACKTICKED), fields(PLAIN))

    def test_HDRS_008_duplicate_critical_field_still_fails(self):
        text = PLAIN.format(work="W", sender="WORKER", recipient="TL",
                            status="COMPLETE", hid="h", seq="1")
        text = text.replace("- Sequence: 1", "- Sequence: 1\n- Work Item: `OTHER`")
        parsed = parse_header(text)
        self.assertFalse(parsed.ok)
        self.assertIn("duplicate-critical-header-field", parsed.reason)

    def test_HDRS_009_wrapping_does_not_change_sender_or_sequence(self):
        got = fields(BACKTICKED, sender="WINDOWS-WORKER", seq="7")
        self.assertEqual(got["from"], "WINDOWS-WORKER")
        self.assertEqual(got["sequence"], "7")


class EngineAcceptanceTests(unittest.TestCase):
    """A genuinely backticked handoff must now route through the real engine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "orbit"
        shutil.copytree(PACKAGE_ROOT / "artifacts", self.root / "artifacts")
        config = json.loads((self.root / "artifacts/live003_bootstrap_config.json").read_text(encoding="utf-8"))
        workspace = self.root / "artifacts/live_trial/M0-WF-LIVE-003"
        if workspace.exists():
            shutil.rmtree(workspace)
        boot = bootstrap_workspace(self.root, config, project_id="Orbit",
                                   workflow_id="orbit-m0-live-trial", work_item="M0-WF-LIVE-003")
        self.manifest = load_manifest(self.root, Path(boot["manifest_path"]))
        self.paths = resolve_runtime_paths(self.root, self.manifest)
        self.paths.inbox.mkdir(parents=True, exist_ok=True)
        self.engine = WorkflowEngine(self.root, self.manifest, PlacePacketExecutor(self.root, self.manifest))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, template: str, **kw) -> Path:
        defaults = dict(work="M0-WF-LIVE-003", sender="WORKER", recipient="TL",
                        status="COMPLETE", hid="bt-1", seq="1")
        defaults.update(kw)
        p = self.paths.inbox / f"HANDOFF_{defaults['work']}_{defaults['sender']}_TO_{defaults['recipient']}.md"
        p.write_text(template.format(**defaults), encoding="utf-8")
        return p

    def test_HDRS_010_backticked_md_handoff_is_accepted(self):
        result = self.engine.process(self.write(BACKTICKED))
        self.assertEqual(result["validation_result"], "accepted", result.get("reason_code"))
        self.assertEqual(result["new_state"]["current_owner_role"], "TL")

    def test_HDRS_011_backticked_zip_handoff_is_accepted(self):
        buf = io.BytesIO()
        body = BACKTICKED.format(work="M0-WF-LIVE-003", sender="WORKER", recipient="TL",
                                 status="COMPLETE", hid="bt-zip-1", seq="1")
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("HANDOFF.md", body)
            archive.writestr("artifacts/evidence.txt", "evidence")
        p = self.paths.inbox / "HANDOFF_M0-WF-LIVE-003_WORKER_TO_TL.zip"
        p.write_bytes(buf.getvalue())

        result = self.engine.process(p)
        self.assertEqual(result["validation_result"], "accepted", result.get("reason_code"))

    def test_HDRS_012_digest_is_of_the_bytes_not_the_unwrapped_value(self):
        p = self.write(BACKTICKED, hid="bt-digest")
        expected = hashlib.sha256(p.read_bytes()).hexdigest()
        result = self.engine.process(p)
        self.assertEqual(result["artifact_digest"], expected)

    def test_HDRS_013_wrong_work_item_still_rejected_when_backticked(self):
        # Unwrapping must not turn a genuine mismatch into a match: the filename
        # says one work item and the header says another.
        p = self.paths.inbox / "HANDOFF_M0-WF-LIVE-003_WORKER_TO_TL.md"
        p.write_text(BACKTICKED.format(work="M0-WF-SOMETHING-ELSE", sender="WORKER",
                                       recipient="TL", status="COMPLETE", hid="x", seq="1"),
                     encoding="utf-8")
        result = self.engine.process(p)
        self.assertEqual(result["validation_result"], "work-item-metadata-mismatch")

    def test_HDRS_014_wrong_sender_still_rejected_when_backticked(self):
        result = self.engine.process(self.write(BACKTICKED, sender="TL", recipient="QA", hid="ws"))
        self.assertNotEqual(result["validation_result"], "accepted")

    def test_HDRS_015_replay_still_rejected_when_backticked(self):
        first = self.engine.process(self.write(BACKTICKED, hid="replay-1"))
        self.assertEqual(first["validation_result"], "accepted")
        again = self.engine.process(self.write(BACKTICKED, hid="replay-1"))
        self.assertIn(again["validation_result"], ("duplicate-replay", "stale-handoff", "unexpected-sender"))

    def test_HDRS_016_unbalanced_backtick_work_item_is_rejected(self):
        p = self.paths.inbox / "HANDOFF_M0-WF-LIVE-003_WORKER_TO_TL.md"
        p.write_text(PLAIN.format(work="`M0-WF-LIVE-003", sender="WORKER", recipient="TL",
                                  status="COMPLETE", hid="u", seq="1"), encoding="utf-8")
        result = self.engine.process(p)
        self.assertEqual(result["validation_result"], "work-item-metadata-mismatch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
