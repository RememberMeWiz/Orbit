"""Typed local executor coverage, weighted toward escape attempts.

The executor is a security boundary, so most of these are adversarial: absolute
paths, traversal in both path flavours, symlink and junction escapes, capability
forgery, and requests for operations that exist as shapes but are not enabled.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from standalone.executor import (
    IMPLEMENTED_OPERATIONS,
    OPERATIONS,
    ExecutorError,
    ExecutorRequest,
    TypedLocalExecutor,
)
from standalone.executor.contracts import MAX_READ_BYTES

try:
    import _winapi
except ImportError:  # pragma: no cover
    _winapi = None


class ExecutorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "approved"
        self.outside = self.base / "outside"
        (self.root / "sub").mkdir(parents=True)
        self.outside.mkdir()

        (self.root / "handoff.md").write_text("# Orbit Handoff\n\nbody\n", encoding="utf-8")
        (self.root / "sub" / "nested.txt").write_text("nested\n", encoding="utf-8")
        (self.outside / "secret.txt").write_text("SECRET-DO-NOT-READ\n", encoding="utf-8")

        self.audit = []
        self.granted = {"WORKER": ("READ_FILE", "LIST_DIRECTORY", "STAT_PATH"), "TL": ("READ_FILE",)}
        self.executor = TypedLocalExecutor(
            self.root,
            capabilities_for_role=lambda role: self.granted.get(role, ()),
            audit=self.audit.append,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, operation="READ_FILE", *, role="WORKER", path="handoff.md", **kw):
        return ExecutorRequest(operation=operation, role=role, task_id="task-1", work_item="WI-1", path=path, **kw)


class HappyPathTests(ExecutorBase):
    def test_EXEC_001_read_file_inside_root(self):
        result = self.executor.execute(self.req())
        self.assertTrue(result.ok)
        self.assertIn("Orbit Handoff", result.data["text"])

    def test_EXEC_002_list_directory(self):
        result = self.executor.execute(self.req("LIST_DIRECTORY", path="."))
        self.assertTrue(result.ok)
        names = {e["name"] for e in result.data["entries"]}
        self.assertEqual(names, {"handoff.md", "sub"})

    def test_EXEC_003_stat_missing_path_is_not_an_error(self):
        result = self.executor.execute(self.req("STAT_PATH", path="nope.txt"))
        self.assertTrue(result.ok)
        self.assertFalse(result.data["exists"])

    def test_EXEC_004_nested_relative_path(self):
        result = self.executor.execute(self.req(path="sub/nested.txt"))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["text"].strip(), "nested")

    def test_EXEC_005_every_call_is_audited(self):
        self.executor.execute(self.req())
        self.executor.execute(self.req(path="../outside/secret.txt"))
        self.assertEqual(len(self.audit), 2)
        self.assertTrue(self.audit[0]["result"]["ok"])
        self.assertFalse(self.audit[1]["result"]["ok"])


class EscapeAttemptTests(ExecutorBase):
    def assert_denied(self, result, reason):
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, reason)
        if result.data:
            self.assertNotIn("SECRET", str(result.data))

    def test_EXEC_010_parent_traversal_denied(self):
        self.assert_denied(self.executor.execute(self.req(path="../outside/secret.txt")), "path-parent-traversal-not-allowed")

    def test_EXEC_011_nested_traversal_denied(self):
        self.assert_denied(self.executor.execute(self.req(path="sub/../../outside/secret.txt")), "path-parent-traversal-not-allowed")

    def test_EXEC_012_posix_absolute_denied(self):
        self.assert_denied(self.executor.execute(self.req(path="/etc/passwd")), "path-absolute-not-allowed")

    def test_EXEC_013_windows_absolute_denied(self):
        self.assert_denied(self.executor.execute(self.req(path=r"C:\Windows\win.ini")), "path-absolute-not-allowed")

    def test_EXEC_014_unc_path_denied(self):
        self.assert_denied(self.executor.execute(self.req(path=r"\\server\share\x.txt")), "path-absolute-not-allowed")

    def test_EXEC_015_backslash_traversal_denied(self):
        self.assert_denied(self.executor.execute(self.req(path=r"..\outside\secret.txt")), "path-parent-traversal-not-allowed")

    def test_EXEC_016_empty_path_denied(self):
        self.assert_denied(self.executor.execute(self.req(path="   ")), "path-missing")

    @unittest.skipUnless(os.name == "nt" and _winapi is not None, "requires Windows junction support")
    def test_EXEC_017_directory_junction_escape_denied(self):
        link = self.root / "escape"
        _winapi.CreateJunction(str(self.outside), str(link))
        result = self.executor.execute(self.req(path="escape/secret.txt"))
        self.assert_denied(result, "path-reparse-point-not-allowed")

    @unittest.skipUnless(os.name == "nt" and _winapi is not None, "requires Windows junction support")
    def test_EXEC_018_listing_through_junction_denied(self):
        link = self.root / "escape"
        _winapi.CreateJunction(str(self.outside), str(link))
        self.assert_denied(self.executor.execute(self.req("LIST_DIRECTORY", path="escape")), "path-reparse-point-not-allowed")

    def test_EXEC_019_symlink_escape_denied_where_supported(self):
        link = self.root / "slink"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this host")
        self.assert_denied(self.executor.execute(self.req(path="slink/secret.txt")), "path-reparse-point-not-allowed")

    def test_EXEC_020_link_inside_listing_is_reported_not_followed(self):
        link = self.root / "slink"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted on this host")
        result = self.executor.execute(self.req("LIST_DIRECTORY", path="."))
        self.assertTrue(result.ok)
        kinds = {e["name"]: e["kind"] for e in result.data["entries"]}
        self.assertEqual(kinds["slink"], "link")


class AuthorityTests(ExecutorBase):
    def test_EXEC_030_capability_not_granted_denied(self):
        # TL holds READ_FILE only.
        result = self.executor.execute(self.req("LIST_DIRECTORY", role="TL", path="."))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "capability-not-granted")

    def test_EXEC_031_unknown_role_has_nothing(self):
        result = self.executor.execute(self.req(role="INTRUDER"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "capability-not-granted")

    def test_EXEC_032_capability_checked_before_path(self):
        """An ungranted role must not learn anything from the path check."""
        result = self.executor.execute(self.req(role="INTRUDER", path="../outside/secret.txt"))
        self.assertEqual(result.reason_code, "capability-not-granted")

    def test_EXEC_033_gated_operations_are_declared_but_refused(self):
        for name in ("WRITE_FILE_IN_APPROVED_ROOT", "RUN_APPROVED_PROCESS", "RUN_APPROVED_TEST", "GIT_STATUS"):
            self.granted["WORKER"] = self.granted["WORKER"] + (name,)
            result = self.executor.execute(self.req(name, path="x"))
            self.assertFalse(result.ok, f"{name} must not execute")
            self.assertEqual(result.reason_code, "operation-not-enabled")

    def test_EXEC_034_unknown_operation_rejected_at_construction(self):
        with self.assertRaises(ExecutorError):
            ExecutorRequest(operation="RUN_COMMAND", role="WORKER", task_id="t", work_item="w")

    def test_EXEC_035_no_generic_command_operation_exists(self):
        names = {s.name for s in OPERATIONS}
        for forbidden in ("RUN_COMMAND", "EXEC", "SHELL", "EVAL", "SYSTEM"):
            self.assertNotIn(forbidden, names)

    def test_EXEC_036_only_read_only_operations_are_implemented(self):
        by_name = {s.name: s for s in OPERATIONS}
        for name in IMPLEMENTED_OPERATIONS:
            self.assertTrue(by_name[name].read_only, f"{name} is implemented but not read-only")


class LimitTests(ExecutorBase):
    def test_EXEC_040_oversized_file_denied(self):
        big = self.root / "big.txt"
        big.write_bytes(b"x" * (MAX_READ_BYTES + 1))
        result = self.executor.execute(self.req(path="big.txt"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "file-too-large")

    def test_EXEC_041_non_utf8_denied(self):
        blob = self.root / "blob.bin"
        blob.write_bytes(b"\xff\xfe\x00binary")
        result = self.executor.execute(self.req(path="blob.bin"))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "file-not-utf8")

    def test_EXEC_042_read_on_directory_denied(self):
        self.assertEqual(self.executor.execute(self.req(path="sub")).reason_code, "path-not-a-file")

    def test_EXEC_043_list_on_file_denied(self):
        self.assertEqual(self.executor.execute(self.req("LIST_DIRECTORY", path="handoff.md")).reason_code, "path-not-a-directory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
