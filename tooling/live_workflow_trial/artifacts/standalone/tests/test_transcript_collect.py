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

# Plain values, no inline code: the accessibility tree splits a backticked
# scalar onto its own line, which would arrive as a malformed header.
HANDOFF = """# Worker Result

## Header
- Work Item: M0-WF-TRANSCRIPT-TEST
- From: WORKER
- To: ORBIT
- Status: COMPLETE
- Handoff ID: M0-WF-TRANSCRIPT-TEST-0001
- Sequence: 1

## Summary
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

    def test_TC_006_a_revised_handoff_supersedes_the_earlier_one(self):
        first = HANDOFF.replace("Sequence: 1", "Sequence: 1\n- Note: first")
        transcript = turn(first) + turn(HANDOFF)
        result = self.collect(transcript)
        self.assertTrue(result.ok, result.reason_code)
        self.assertNotIn("first", Path(result.data["path"]).read_text(encoding="utf-8"))


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

    def test_TC_022_a_missing_header_is_refused_and_leaves_no_file(self):
        result = self.collect(turn("just some prose, no header at all\n"))
        self.assertFalse(result.ok)
        self.assertIn("missing-formal-header", result.reason_code)
        self.assertFalse((self.inbox / ARTIFACT).exists())

    def test_TC_023_a_header_for_another_work_item_is_refused(self):
        wrong = HANDOFF.replace("- Work Item: M0-WF-TRANSCRIPT-TEST",
                                "- Work Item: SOME-OTHER-ITEM")
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

    def test_TC_029_an_invalid_body_does_not_stay_in_the_inbox(self):
        """A rejected file left behind would look collected to the next run."""
        self.collect(turn("no header here\n"))
        self.assertFalse((self.inbox / ARTIFACT).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
