from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Tuple

from workflow.core.storage import atomic_write_json, is_within


class PlacePacketExecutor:
    """Bounded Windows-oriented executor exposing only PLACE_PACKET.

    Destination paths are configuration-owned and must remain beneath the
    package artifacts root. Handoff-controlled identifiers never become path
    components directly; filenames use a digest-derived token instead.

    ``last_path_decision`` / ``path_decisions`` are QA observability only. They
    do not add executor authority or alter the shared workflow contract.
    """

    def __init__(self, root: Path, manifest: Dict[str, Any], fail_next: bool = False):
        self.root = root
        self.manifest = manifest
        self.fail_next = fail_next
        self.operations: list[str] = []
        self.path_decisions: list[Dict[str, Any]] = []
        self.last_path_decision: Dict[str, Any] | None = None

    def _record_path_decision(
        self,
        *,
        observed_path: str,
        resolved_path: str,
        configured_root: str,
        within_allowed_root: bool,
        reparse_point_encountered: bool,
        decision: str,
        reason_code: str | None,
    ) -> None:
        record = {
            "observed_path": observed_path,
            "resolved_path": resolved_path,
            "configured_root": configured_root,
            "within_allowed_root": bool(within_allowed_root),
            "reparse_point_encountered": bool(reparse_point_encountered),
            "decision": decision,
            "reason_code": reason_code,
        }
        self.last_path_decision = record
        self.path_decisions.append(record)

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            is_junction = getattr(path, "is_junction", None)
            return bool(callable(is_junction) and is_junction())
        except OSError:
            return True

    def _path_has_reparse(self, artifacts_root: Path, candidate: Path) -> bool:
        """Check existing path components without granting authority to links.

        On Windows, ``Path.is_junction`` covers directory junctions. Symlinks
        are rejected on every platform. Missing components are not links yet;
        the destination is revalidated immediately before packet placement.
        """
        if self._is_reparse(artifacts_root):
            return True
        try:
            relative = candidate.relative_to(artifacts_root)
        except ValueError:
            return True
        current = artifacts_root
        for part in relative.parts:
            current = current / part
            if current.exists() and self._is_reparse(current):
                return True
        return False

    def _resolve_destination(self, destination_key: str) -> Tuple[Path | None, str | None]:
        artifacts_root = (self.root / "artifacts").resolve()
        configured_root = str(artifacts_root)
        registry = self.manifest.get("role_destination_registry", {})
        entry = registry.get(destination_key)

        if destination_key not in self.manifest.get("destinations", {}) or entry is None:
            self._record_path_decision(
                observed_path=str(destination_key),
                resolved_path="",
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=False,
                decision="DENY",
                reason_code="destination-not-allowlisted",
            )
            return None, "destination-not-allowlisted"

        if not bool(entry.get("enabled", False)):
            self._record_path_decision(
                observed_path=str(entry.get("endpoint_ref", self.manifest["destinations"][destination_key])),
                resolved_path="",
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=False,
                decision="DENY",
                reason_code="destination-disabled",
            )
            return None, "destination-disabled"

        if entry.get("adapter_type") != "PLACE_PACKET":
            self._record_path_decision(
                observed_path=str(entry.get("endpoint_ref", "")),
                resolved_path="",
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=False,
                decision="DENY",
                reason_code="destination-adapter-not-place-packet",
            )
            return None, "destination-adapter-not-place-packet"

        rel = str(entry.get("endpoint_ref", ""))
        if rel != str(self.manifest["destinations"][destination_key]):
            self._record_path_decision(
                observed_path=rel,
                resolved_path="",
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=False,
                decision="DENY",
                reason_code="destination-registry-config-mismatch",
            )
            return None, "destination-registry-config-mismatch"

        win = PureWindowsPath(rel)
        native = Path(rel)
        if win.is_absolute() or win.drive or native.is_absolute():
            self._record_path_decision(
                observed_path=rel,
                resolved_path="",
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=False,
                decision="DENY",
                reason_code="destination-absolute-path-not-allowed",
            )
            return None, "destination-absolute-path-not-allowed"
        if ".." in win.parts or ".." in native.parts:
            self._record_path_decision(
                observed_path=rel,
                resolved_path="",
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=False,
                decision="DENY",
                reason_code="destination-parent-traversal-not-allowed",
            )
            return None, "destination-parent-traversal-not-allowed"

        unresolved = artifacts_root / native
        reparse = self._path_has_reparse(artifacts_root, unresolved)
        try:
            destination = unresolved.resolve()
        except OSError:
            self._record_path_decision(
                observed_path=rel,
                resolved_path="",
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=reparse,
                decision="DENY",
                reason_code="destination-resolution-failed",
            )
            return None, "destination-resolution-failed"

        within = is_within(destination, artifacts_root)
        if reparse:
            self._record_path_decision(
                observed_path=rel,
                resolved_path=str(destination),
                configured_root=configured_root,
                within_allowed_root=within,
                reparse_point_encountered=True,
                decision="DENY",
                reason_code="destination-reparse-point-not-allowed",
            )
            return None, "destination-reparse-point-not-allowed"
        if not within:
            self._record_path_decision(
                observed_path=rel,
                resolved_path=str(destination),
                configured_root=configured_root,
                within_allowed_root=False,
                reparse_point_encountered=False,
                decision="DENY",
                reason_code="destination-outside-artifacts-root",
            )
            return None, "destination-outside-artifacts-root"

        self._record_path_decision(
            observed_path=rel,
            resolved_path=str(destination),
            configured_root=configured_root,
            within_allowed_root=True,
            reparse_point_encountered=False,
            decision="ALLOW",
            reason_code=None,
        )
        return destination, None

    def place_packet(self, destination_key: str, packet: Dict[str, Any]) -> Tuple[bool, str, str]:
        operation = "PLACE_PACKET"
        if self.manifest.get("allowed_executor_operations") != ["PLACE_PACKET"]:
            return False, "FAILED_FINAL:executor-catalog-not-exactly-place-packet", "none"
        if operation not in self.manifest["allowed_executor_operations"]:
            return False, "FAILED_FINAL:operation-not-allowlisted", "none"

        destination, error = self._resolve_destination(destination_key)
        if error:
            return False, f"FAILED_FINAL:{error}", "none"
        assert destination is not None

        self.operations.append(operation)
        if self.fail_next:
            self.fail_next = False
            return False, "FAILED_RETRYABLE:injected-executor-failure", "local-place-packet"

        # Revalidate after directory creation so a path changed between the
        # authorization decision and placement cannot silently retain authority.
        destination.mkdir(parents=True, exist_ok=True)
        destination, error = self._resolve_destination(destination_key)
        if error:
            return False, f"FAILED_FINAL:{error}", "none"
        assert destination is not None

        opaque = str(packet["handoff_id"]).encode("utf-8")
        token = hashlib.sha256(opaque).hexdigest()[:24]
        out = (destination / f"NEXT_{token}_{destination_key}.json").resolve()
        if not is_within(out, destination) or self._path_has_reparse((self.root / "artifacts").resolve(), out.parent):
            self._record_path_decision(
                observed_path=str(out),
                resolved_path=str(out),
                configured_root=str(destination),
                within_allowed_root=False,
                reparse_point_encountered=True,
                decision="DENY",
                reason_code="output-path-escaped-or-reparse-destination",
            )
            return False, "FAILED_FINAL:output-path-escaped-or-reparse-destination", "none"
        atomic_write_json(out, packet)
        return True, "PREPARED", str(out)
