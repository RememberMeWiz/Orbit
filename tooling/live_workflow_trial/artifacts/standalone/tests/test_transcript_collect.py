"""Collecting a handoff the worker wrote into the conversation.

This path exists for economy. Asking a worker for a *downloadable file* is what
makes the app offer to escalate into a paid work mode, so for a plain text
handoff it costs credits to obtain something the conversation could simply have
contained. Nothing about the checks is relaxed to buy that: the bytes go to the
same inbox and through the same validator as a saved file.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from standalone.tests.test_chatgpt_adapter import StubDriver, build, ok

WORK_ITEM = "M0-WF-TRANSCRIPT-TEST"
ARTIFACT = "HANDOFF_M0-WF-TRANSCRIPT-TEST_WORKER_TO_ORBIT.md"

# Flat fields, not Markdown. The accessibility tree keeps plain text and drops
# structure: "## Header" arrives as "Header" and the bullet list under it
# disappears, which is exactly how the first live attempt failed.
HANDOFF = """work_item: M0-WF-TRANSCRIPT-TEST
from: WORKER
to: ORBIT
status: COMPLETE
handoff_id: M0-WF-TRANSCRIPT-TEST-0001
sequence: 1
ORBIT_HANDOFF_BODY
Returned in the conversation. No file, no work mode, no credits.
"""


def turn(body: str, *, name: str = ARTIFACT, author: str = "ChatGPT said:") -> str:
    return f"{author}\nHere you go.\nORBIT_HANDOFF_BEGIN {name}\n{body}ORBIT_HANDOFF_END\n"


class TranscriptDriver(StubDriver):
    def __init__(self, transcript: str, **kw):
        super().__init__(**kw)
        self.transcript = transcript

    def read_transcript_tail(self, max_chars=6000):
        self.calls.append("read_transcript_tail")
        return ok({"text": self.transcript, "nodes": 1, "total_length": len(self.transcript)})

    def call(self, operation, params=None):
        if operation == "read_transcript_tail":
            return self.read_transcript_tail(**(params or {}))
        return getattr(self, operation)()


class CollectBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, transcript, *, expect=ARTIFACT, sender="", work_item=WORK_ITEM):
        adapter = build(TranscriptDriver(transcript))
        return adapter.collect_from_transcript(
            endpoint_id="windows-workflow", expected_name=expect,
            inbox_dir=self.inbox, work_item=work_item, expected_sender=sender)


class HappyPathTests(CollectBase):
    def test_TC_001_a_handoff_in_the_conversation_is_collected(self):
        result = self.collect(turn(HANDOFF))
        self.assertTrue(result.ok, result.reason_code)
        self.assertEqual(result.data["filename"], ARTIFACT)
        self.assertEqual(result.data["source"], "transcript")

    def test_TC_002_it_is_written_to_the_inbox_and_hashed(self):
        import hashlib
        result = self.collect(turn(HANDOFF))
        path = Path(result.data["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(result.data["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_TC_003_the_header_is_parsed_the_same_as_a_saved_file(self):
        result = self.collect(turn(HANDOFF))
        self.assertEqual(result.data["work_item"], WORK_ITEM)
        self.assertEqual(result.data["sender"], "WORKER")
        self.assertEqual(result.data["status"], "COMPLETE")
        self.assertEqual(result.data["sequence"], "1")

    def test_TC_004_no_file_card_is_ever_saved(self):
        """The whole point: nothing asks the app to produce a file."""
        driver = TranscriptDriver(turn(HANDOFF))
        build(driver).collect_from_transcript(
            endpoint_id="windows-workflow", expected_name=ARTIFACT,
            inbox_dir=self.inbox, work_item=WORK_ITEM)
        self.assertNotIn("save_artifact_as", driver.calls)
        self.assertNotIn("list_artifacts", driver.calls)

    def test_TC_005_the_endpoint_is_focused_and_verified_first(self):
        driver = TranscriptDriver(turn(HANDOFF))
        build(driver).collect_from_transcript(
            endpoint_id="windows-workflow", expected_name=ARTIFACT,
            inbox_dir=self.inbox, work_item=WORK_ITEM)
        self.assertIn("focus_chat:Windows Workflow", driver.calls)

    def test_TC_006_a_second_eligible_block_is_ambiguous_not_a_revision(self):
        """Recency proves the text came later, not that it is the handoff.

        A worker that quotes, echoes, revises or demonstrates a handoff produces
        a second complete candidate inside an assistant turn, where provenance
        filtering cannot tell it apart from the real one.
        """
        first = HANDOFF.replace("No file, no work mode", "first draft")
        result = self.collect(turn(first) + turn(HANDOFF))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-ambiguous")
        self.assertIn("eligible blocks", result.detail)

    def test_TC_007_two_candidates_in_one_turn_are_also_ambiguous(self):
        body = f"ORBIT_HANDOFF_BEGIN {ARTIFACT}\n{HANDOFF}ORBIT_HANDOFF_END\n"
        result = self.collect("ChatGPT said:\nAs an example:\n" + body + "And the real one:\n" + body)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-ambiguous")

    def test_TC_008_collection_records_what_it_saw(self):
        result = self.collect(turn(HANDOFF))
        self.assertEqual(result.data["candidates_seen"], 1)
        self.assertEqual(result.data["collected_from"], "windows-workflow")
        self.assertEqual(result.data["rejected_candidates"], [])


class ProvenanceTests(CollectBase):
    def test_TC_010_orbits_own_assignment_is_not_collectable(self):
        """The assignment names the file, so without turn scoping Orbit would
        find its own instructions and collect them."""
        result = self.collect(turn(HANDOFF, author="You said:"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-not-found")

    def test_TC_011_a_transcript_without_turns_yields_nothing(self):
        result = self.collect("ORBIT_HANDOFF_BEGIN " + ARTIFACT + "\n" + HANDOFF + "ORBIT_HANDOFF_END\n")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-not-found")


class RefusalTests(CollectBase):
    def test_TC_020_a_block_named_something_else_is_not_the_expected_one(self):
        result = self.collect(turn(HANDOFF, name="HANDOFF_M0-WF-TRANSCRIPT-TEST_QA_TO_ORBIT.md"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-not-found")

    def test_TC_021_an_unterminated_block_is_not_a_handoff(self):
        """A cut-off message has not delivered anything."""
        truncated = f"ChatGPT said:\nORBIT_HANDOFF_BEGIN {ARTIFACT}\n{HANDOFF}"
        result = self.collect(truncated)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-not-found")

    def test_TC_022_a_block_with_no_fields_is_not_a_handoff(self):
        """Orbit renders the header it was given; it never invents one."""
        result = self.collect(turn("just some prose, no fields at all\n"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-not-found")
        self.assertFalse((self.inbox / ARTIFACT).exists())

    def test_TC_022b_a_block_missing_one_critical_field_is_refused(self):
        for dropped in ("work_item", "from", "to", "status", "handoff_id", "sequence"):
            partial = "\n".join(l for l in HANDOFF.splitlines()
                                if not l.startswith(dropped + ":")) + "\n"
            result = self.collect(turn(partial))
            self.assertFalse(result.ok, dropped)
            self.assertEqual(result.reason_code, "transcript-handoff-not-found", dropped)

    def test_TC_023_a_header_for_another_work_item_is_refused(self):
        wrong = HANDOFF.replace("work_item: M0-WF-TRANSCRIPT-TEST",
                                "work_item: SOME-OTHER-ITEM")
        result = self.collect(turn(wrong))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-header-work-item-mismatch")

    def test_TC_024_a_sender_mismatch_is_refused(self):
        result = self.collect(turn(HANDOFF), sender="QA")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-sender-mismatch")

    def test_TC_025_a_filename_that_is_not_handoff_shaped_is_refused(self):
        result = self.collect(turn(HANDOFF, name="notes.md"), expect="notes.md")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-name-not-handoff-shaped")

    def test_TC_026_a_filename_declaring_another_work_item_is_refused(self):
        other = "HANDOFF_SOMETHING-ELSE_WORKER_TO_ORBIT.md"
        result = self.collect(turn(HANDOFF, name=other), expect=other)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-work-item-mismatch")

    def test_TC_027_a_zip_cannot_come_through_the_transcript(self):
        name = "HANDOFF_M0-WF-TRANSCRIPT-TEST_WORKER_TO_ORBIT.zip"
        result = self.collect(turn(HANDOFF, name=name), expect=name)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "artifact-transcript-requires-markdown")

    def test_TC_028_an_already_collected_artifact_is_never_overwritten(self):
        self.assertTrue(self.collect(turn(HANDOFF)).ok)
        again = self.collect(turn(HANDOFF))
        self.assertFalse(again.ok)
        self.assertEqual(again.reason_code, "artifact-already-collected")

    def test_TC_029_a_body_rejected_by_the_validator_does_not_stay_in_the_inbox(self):
        """It is written before it is validated, so it must be removed after.

        A rejected file left behind would look collected to the next run, and
        `artifact-already-collected` would then refuse the real one.
        """
        wrong = HANDOFF.replace("work_item: M0-WF-TRANSCRIPT-TEST",
                                "work_item: SOME-OTHER-ITEM")
        result = self.collect(turn(wrong))
        self.assertEqual(result.reason_code, "artifact-header-work-item-mismatch")
        self.assertFalse((self.inbox / ARTIFACT).exists())
        # ...and the real one can still be collected afterwards.
        self.assertTrue(self.collect(turn(HANDOFF)).ok)

    def test_TC_030_markdown_structure_is_never_asked_of_the_worker(self):
        """The channel drops headings and bullets, so Orbit writes them itself."""
        import standalone.bridge.chatgpt as mod

        rendered, problems = mod._handoff_candidates(
            f"ORBIT_HANDOFF_BEGIN {ARTIFACT}\n{HANDOFF}ORBIT_HANDOFF_END\n", ARTIFACT)
        self.assertEqual(problems, [])
        self.assertIn("## Header", rendered[0])
        self.assertIn("- Work Item: M0-WF-TRANSCRIPT-TEST", rendered[0])
        # None of that Markdown came from the worker's own text.
        self.assertNotIn("## Header", HANDOFF)
        self.assertNotIn("- Work Item", HANDOFF)

    def test_TC_031_an_end_marker_inside_the_body_is_refused(self):
        """A body discussing the protocol would otherwise truncate itself.

        The header stays valid, so validation passes and the findings are
        silently lost — the worst kind of failure.
        """
        # The marker alone on its own line is the realistic hazard: a body that
        # quotes the protocol truncates the handoff after a still-valid header.
        talkative = HANDOFF + "as in:\nORBIT_HANDOFF_END\nmore findings\n"
        result = self.collect(turn(talkative))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "transcript-handoff-not-found")
        self.assertIn("embedded-end-marker", result.detail)

    def test_TC_032_a_second_body_marker_is_refused(self):
        confused = HANDOFF + "ORBIT_HANDOFF_BODY\nwhich half is the body?\n"
        result = self.collect(turn(confused))
        self.assertFalse(result.ok)
        self.assertIn("embedded-body-marker", result.detail)

    def test_TC_033_a_rejected_body_is_quarantined_with_its_reason(self):
        """The only reconstruction is the evidence; deleting it destroys that."""
        import json

        wrong = HANDOFF.replace("work_item: M0-WF-TRANSCRIPT-TEST",
                                "work_item: SOME-OTHER-ITEM")
        self.collect(turn(wrong))
        quarantine = self.inbox.parent / "quarantine"
        rejected = list(quarantine.glob(f"{ARTIFACT}.*.rejected"))
        reasons = list(quarantine.glob(f"{ARTIFACT}.*.reason.json"))
        self.assertEqual(len(rejected), 1)
        self.assertIn("SOME-OTHER-ITEM", rejected[0].read_text(encoding="utf-8"))
        payload = json.loads(reasons[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["reason_code"], "artifact-header-work-item-mismatch")
        self.assertEqual(payload["endpoint_id"], "windows-workflow")

    def test_TC_034_quarantine_is_outside_the_inbox(self):
        """Anything inside the inbox could be mistaken for authority."""
        wrong = HANDOFF.replace("work_item: M0-WF-TRANSCRIPT-TEST",
                                "work_item: SOME-OTHER-ITEM")
        self.collect(turn(wrong))
        self.assertEqual(list(self.inbox.glob("*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
