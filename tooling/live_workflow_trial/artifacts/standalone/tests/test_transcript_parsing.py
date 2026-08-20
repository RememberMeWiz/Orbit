"""Reading PM's decision out of a real transcript.

A transcript is not a message. It is an append-only log of a whole conversation
in which the envelope marker appears many times: in prose, in the reply template
Orbit itself posted, and finally in PM's actual answer. On top of that, the
accessibility tree renders a syntax-highlighted code block as one text node per
token, so the older copies come back fragmented across lines.

Every case here is reconstructed from the structure of the first live B1 attempt,
where the parser took the *first* marker — Orbit's own template — and reported
PM as having answered badly while PM's correct answer sat further down.
"""
from __future__ import annotations

import unittest

from standalone.bridge.pm_envelope import PMDirective, parse_envelope

REQUEST_ID = "pmreq-9ae12ea75546663186d7"
WORK_ITEM = "M0-WF-B1-LIVE-001"

# Orbit's own reply template, as the accessibility tree renders it back: the
# code block is syntax-highlighted, so each token becomes its own line.
ECHOED_TEMPLATE = (
    "ORBIT_DIRECTIVE\n\n\nversion:\n \n0.1\n\n\n"
    f"request_id:\n pmreq-\n9\nae12ea75546663186d7\n\n\n"
    "directive_id:\n <\nnew\n unique id \nfor\n this decision>\n\n\n"
    "work_item:\n M0-WF-B1-LIVE-\n001\n\n\n"
    "action:\n <DISPATCH_TO_ROLE | HOLD | \nSTOP\n>\n\n\n"
    "target_endpoint:\n <architecture-tl | orbit-pm | product-research"
    " | qa-safety | windows-worker>\n"
)

# PM's actual answer, as the same tree renders it: clean fields, blank lines
# between them.
PM_ANSWER = (
    "ORBIT_DIRECTIVE\n\n\nversion: 0.1\n\n\n"
    f"request_id: {REQUEST_ID}\n\n\n"
    "directive_id: pmdir-20260820-1930-b1-live-001\n\n\n"
    f"work_item: {WORK_ITEM}\n\n\n"
    "action: DISPATCH_TO_ROLE\n\n\n"
    "target_endpoint: windows-worker\n"
)

PROSE_BEFORE = (
    "This is still the Orbit PM endpoint. For anything that should change\n"
    "workflow state, use the governed ORBIT_PM_REQUEST / ORBIT_DIRECTIVE\n"
    "envelope. Plain chat like this is conversation only.\n"
    "You said:\n"
    "ORBIT_PM_REQUEST\nversion: 0.1\n"
    f"request_id: {REQUEST_ID}\nwork_item: {WORK_ITEM}\n"
    "current_owner: ORBIT\nawaiting: ORBIT_DIRECTIVE\n"
    "Reply with this envelope in a fenced code block:\n"
)

PROSE_AFTER = "\n\nMessage ChatGPT\nOrbit — some later pasted document\n"

LIVE_TRANSCRIPT = PROSE_BEFORE + ECHOED_TEMPLATE + "ChatGPT said:\nPlain text\n" + PM_ANSWER + PROSE_AFTER


class LiveTranscriptTests(unittest.TestCase):
    """The exact shape that failed the first live B1 attempt."""

    def test_TR_001_pm_answer_is_found_past_orbits_own_template(self):
        directive, reason = parse_envelope(LIVE_TRANSCRIPT)
        self.assertIsNotNone(directive, reason)
        self.assertEqual(directive.directive_id, "pmdir-20260820-1930-b1-live-001")
        self.assertEqual(directive.request_id, REQUEST_ID)
        self.assertEqual(directive.work_item, WORK_ITEM)
        self.assertEqual(directive.action, "DISPATCH_TO_ROLE")
        self.assertEqual(directive.target_endpoint, "windows-worker")

    def test_TR_002_the_echoed_template_alone_is_never_a_directive(self):
        """Orbit must not take instruction from its own message."""
        directive, reason = parse_envelope(PROSE_BEFORE + ECHOED_TEMPLATE)
        self.assertIsNone(directive)
        self.assertNotEqual(reason, "directive-parsed")

    def test_TR_003_an_unedited_template_is_named_as_such(self):
        clean_template = (
            "ORBIT_DIRECTIVE\nversion: 0.1\n"
            f"request_id: {REQUEST_ID}\n"
            "directive_id: <new unique id for this decision>\n"
            f"work_item: {WORK_ITEM}\n"
            "action: <DISPATCH_TO_ROLE | HOLD | STOP>\n"
            "target_endpoint: <windows-worker>\n"
        )
        directive, reason = parse_envelope(clean_template)
        self.assertIsNone(directive)
        self.assertTrue(reason.startswith("directive-template-not-filled-in:"), reason)
        for placeheld in ("action", "directive_id", "target_endpoint"):
            self.assertIn(placeheld, reason)

    def test_TR_004_prose_mentioning_the_marker_is_not_authority(self):
        directive, reason = parse_envelope(
            "Use the ORBIT_DIRECTIVE envelope when you want something done.")
        self.assertIsNone(directive)
        self.assertEqual(reason, "directive-absent")


class NewestWinsTests(unittest.TestCase):
    def envelope(self, directive_id, *, action="DISPATCH_TO_ROLE", target="windows-worker"):
        return ("\nORBIT_DIRECTIVE\nversion: 0.1\n"
                f"request_id: {REQUEST_ID}\ndirective_id: {directive_id}\n"
                f"work_item: {WORK_ITEM}\naction: {action}\n"
                f"target_endpoint: {target}\n")

    def test_TR_010_the_most_recent_decision_wins(self):
        """PM changing their mind must not be overridden by the older message."""
        text = self.envelope("dir-first", target="qa-safety") + \
            "\nActually, on reflection:\n" + self.envelope("dir-second", target="windows-worker")
        directive, _ = parse_envelope(text)
        self.assertEqual(directive.directive_id, "dir-second")
        self.assertEqual(directive.target_endpoint, "windows-worker")

    def test_TR_011_a_malformed_newer_candidate_does_not_mask_a_valid_older_one(self):
        text = self.envelope("dir-good") + "\nORBIT_DIRECTIVE\nversion: 0.1\nnotes: oops\n"
        directive, _ = parse_envelope(text)
        self.assertIsNotNone(directive)
        self.assertEqual(directive.directive_id, "dir-good")

    def test_TR_012_when_nothing_parses_the_newest_reason_is_reported(self):
        """The newest candidate is the one PM most likely just wrote."""
        text = ("\nORBIT_DIRECTIVE\nversion: 0.1\nrequest_id: x\n"
                "directive_id: d\nwork_item: w\naction: SOMETHING_ELSE\n")
        directive, reason = parse_envelope(text)
        self.assertIsNone(directive)
        self.assertEqual(reason, "directive-action-not-allowlisted:SOMETHING_ELSE")

    def test_TR_013_a_fenced_block_still_parses(self):
        text = "```\n" + self.envelope("dir-fenced").strip() + "\n```"
        directive, _ = parse_envelope(text)
        self.assertEqual(directive.directive_id, "dir-fenced")

    def test_TR_014_the_last_fenced_block_wins_too(self):
        text = ("```\n" + self.envelope("dir-old").strip() + "\n```\n"
                "```\n" + self.envelope("dir-new").strip() + "\n```")
        directive, _ = parse_envelope(text)
        self.assertEqual(directive.directive_id, "dir-new")

    def test_TR_015_an_empty_transcript_is_absent_not_an_error(self):
        self.assertEqual(parse_envelope(""), (None, "directive-absent"))


class StillRefusedTests(unittest.TestCase):
    """Scanning newest-first must not have loosened any existing refusal."""

    def base(self, **kw):
        fields = {"version": "0.1", "request_id": REQUEST_ID, "directive_id": "d-1",
                  "work_item": WORK_ITEM, "action": "DISPATCH_TO_ROLE",
                  "target_endpoint": "windows-worker"}
        fields.update(kw)
        return "\nORBIT_DIRECTIVE\n" + "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n"

    def test_TR_020_unsupported_version_is_refused(self):
        directive, reason = parse_envelope(self.base(version="9.9"))
        self.assertIsNone(directive)
        self.assertEqual(reason, "directive-version-unsupported:9.9")

    def test_TR_021_action_outside_the_allowlist_is_refused(self):
        directive, reason = parse_envelope(self.base(action="RUN_COMMAND"))
        self.assertIsNone(directive)
        self.assertEqual(reason, "directive-action-not-allowlisted:RUN_COMMAND")

    def test_TR_022_a_missing_required_field_is_refused(self):
        text = ("\nORBIT_DIRECTIVE\nversion: 0.1\n"
                f"request_id: {REQUEST_ID}\nwork_item: {WORK_ITEM}\n")
        directive, reason = parse_envelope(text)
        self.assertIsNone(directive)
        self.assertTrue(reason.startswith("directive-missing:"), reason)

    def test_TR_023_a_valid_envelope_is_still_only_a_proposal(self):
        """Parsing says the shape is right, never that the decision applies."""
        directive, reason = parse_envelope(self.base())
        self.assertIsInstance(directive, PMDirective)
        self.assertEqual(reason, "directive-parsed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
