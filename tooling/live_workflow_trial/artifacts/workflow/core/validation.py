from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from workflow.contracts import HeaderParseResult, ValidationResult
from .storage import bytes_digest, is_within

ALLOWED_STATUSES = {"COMPLETE", "BLOCKED", "NEEDS_DECISION", "REQUEST_CHANGES", "REQUEST_WORKER"}
NAME_RE = re.compile(r"^HANDOFF_(?P<work>.+?)_(?P<sender>[A-Z][A-Z0-9-]*)_TO_(?P<recipient>[A-Z][A-Z0-9-]*)\.(?P<ext>md|zip)$")
HEADER_RE = re.compile(r"^\s*##\s+Header\s*$", re.IGNORECASE)
SECTION_RE = re.compile(r"^\s*#{1,6}\s+\S")
HEADER_FIELD_RE = re.compile(r"^\s*-\s*([^:]+):\s*(.*?)\s*$")
CRITICAL_HEADER_FIELDS = {"work item", "from", "to", "status", "handoff id", "sequence"}


def parse_header(text: str) -> HeaderParseResult:
    lines = text.splitlines()
    start: Optional[int] = None
    for index, line in enumerate(lines):
        if HEADER_RE.match(line):
            start = index + 1
            break
    if start is None:
        return HeaderParseResult(False, "missing-formal-header", {})

    fields: Dict[str, str] = {}
    for line in lines[start:]:
        if SECTION_RE.match(line):
            break
        if not line.strip():
            continue
        match = HEADER_FIELD_RE.match(line)
        if not match:
            return HeaderParseResult(False, "malformed-header-line", fields)
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if key in CRITICAL_HEADER_FIELDS and key in fields:
            return HeaderParseResult(False, f"duplicate-critical-header-field:{key}", fields)
        if key not in fields:
            fields[key] = value
    return HeaderParseResult(True, "header-parsed", fields)


def _read_bytes_without_write_race(path: Path) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError:
        return None, "unreadable-artifact"
    before_sig = (before.st_size, before.st_mtime_ns)
    after_sig = (after.st_size, after.st_mtime_ns)
    if before_sig != after_sig or after.st_size != len(data):
        return None, "artifact-changed-during-read"
    return data, None


class HandoffValidator:
    def __init__(self, manifest: Dict[str, Any], approved_inbox: Path):
        self.manifest = manifest
        self.approved_inbox = approved_inbox

    @staticmethod
    def _reject(reason: str, **metadata: Any) -> ValidationResult:
        return ValidationResult(False, reason, metadata or None)

    def _check_source_boundary(self, path: Path) -> Optional[str]:
        try:
            if path.is_symlink():
                return "source-link-not-allowed"
            is_junction = getattr(path, "is_junction", None)
            if callable(is_junction) and is_junction():
                return "source-junction-not-allowed"
            if not is_within(path, self.approved_inbox):
                return "source-outside-approved-inbox"
        except OSError:
            return "source-boundary-check-failed"
        return None

    def _read_payload(self, path: Path, ext: str) -> ValidationResult:
        data, error = _read_bytes_without_write_race(path)
        if error:
            return ValidationResult(False, error)
        assert data is not None
        digest = bytes_digest(data)

        if ext == "md":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return ValidationResult(False, "unreadable-markdown")
            return ValidationResult(True, "payload-read", {"text": text, "artifact_digest": digest, "size_bytes": len(data)})

        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                names = zf.namelist()
                if "HANDOFF.md" not in names:
                    return ValidationResult(False, "zip-missing-root-HANDOFF.md")
                for name in names:
                    normalized = name.replace("\\", "/")
                    if normalized.startswith("/") or "../" in normalized or normalized == "..":
                        return ValidationResult(False, "zip-path-traversal")
                    if normalized != "HANDOFF.md" and not normalized.startswith("artifacts/"):
                        return ValidationResult(False, "zip-unsupported-root-entry")
                text = zf.read("HANDOFF.md").decode("utf-8")
                return ValidationResult(True, "payload-read", {
                    "text": text,
                    "zip_names": names,
                    "artifact_digest": digest,
                    "size_bytes": len(data),
                })
        except (zipfile.BadZipFile, UnicodeDecodeError, OSError):
            return ValidationResult(False, "malformed-zip")

    def validate(self, path: Path, state: Dict[str, Any]) -> ValidationResult:
        boundary_error = self._check_source_boundary(path)
        if boundary_error:
            return ValidationResult(False, boundary_error)

        match = NAME_RE.match(path.name)
        if not match:
            return ValidationResult(False, "unsupported-filename")
        groups = match.groupdict()
        if groups["work"] != self.manifest["work_item"]:
            return ValidationResult(False, "wrong-work-item")

        payload = self._read_payload(path, groups["ext"])
        if not payload.ok:
            return payload
        assert payload.metadata is not None

        parsed = parse_header(payload.metadata["text"])
        if not parsed.ok:
            return ValidationResult(False, parsed.reason, {"parsed_header": parsed.fields})
        fields = parsed.fields
        required = ["work item", "from", "to", "status", "handoff id", "sequence"]
        missing = [k for k in required if not fields.get(k)]
        if missing:
            return ValidationResult(False, "missing-metadata:" + ",".join(missing), {"parsed_header": fields})

        if fields["work item"] != groups["work"]:
            return ValidationResult(False, "work-item-metadata-mismatch")
        if fields["from"].upper() != groups["sender"]:
            return ValidationResult(False, "sender-metadata-mismatch")
        if fields["to"].upper() != groups["recipient"]:
            return ValidationResult(False, "recipient-metadata-mismatch")
        if fields["status"].upper() not in ALLOWED_STATUSES:
            return ValidationResult(False, "unsupported-status")
        if groups["recipient"] not in self.manifest["roles"]:
            return ValidationResult(False, "unknown-recipient")

        try:
            sequence = int(fields["sequence"])
        except ValueError:
            return ValidationResult(False, "invalid-sequence")
        if sequence <= 0:
            return ValidationResult(False, "invalid-sequence")

        handoff_id = fields["handoff id"]
        digest = payload.metadata["artifact_digest"]
        common = {
            "handoff_id": handoff_id,
            "sequence": sequence,
            "status": fields["status"].upper(),
            "sender": groups["sender"],
            "recipient": groups["recipient"],
            "artifact_digest": digest,
            "size_bytes": payload.metadata["size_bytes"],
            "source_path": str(path),
        }

        if handoff_id in state.get("accepted_handoff_ids", []):
            accepted_digest = state.get("accepted_handoff_digests", {}).get(handoff_id)
            if accepted_digest and accepted_digest != digest:
                return self._reject("replay-digest-mismatch", **common, accepted_artifact_digest=accepted_digest)
            return self._reject("duplicate-replay", **common, accepted_artifact_digest=accepted_digest)

        if sequence <= int(state.get("last_sequence", 0)):
            return self._reject("stale-handoff", **common)

        if groups["sender"] != state["current_owner_role"]:
            return self._reject("unexpected-sender", **common)
        expected_recipient = self.manifest["valid_transitions"].get(state["current_owner_role"])
        if groups["recipient"] != expected_recipient:
            return self._reject("wrong-recipient", **common)

        return ValidationResult(True, "accepted", common)
