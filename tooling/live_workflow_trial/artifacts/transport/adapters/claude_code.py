"""Claude Code local adapter.

Built against the contract the installed CLI actually documents (`claude --help`,
`claude agents --help` on 2.1.234), not assumed flags:

* ``-p/--print`` runs non-interactively and exits;
* ``--output-format json`` returns one result envelope carrying ``session_id``,
  ``is_error``, ``result``, ``subtype`` and ``permission_denials``;
* ``--session-id <uuid>`` lets the *caller* supply the session identity, which is
  what makes exact-once correlation possible;
* ``claude agents --json`` lists sessions for scripting without a TTY.

Authority boundary
------------------
The executable path is fixed when the adapter is constructed from trusted
transport configuration. It is never read from a manifest, an assignment, or
handoff prose. The process is spawned with an argument *list* and no shell, so
prompt text is inert data that cannot become argv or a command line. Every
capability flag is derived from the registered endpoint alone, so an assignment
can narrow authority but never widen it.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from workflow.core.storage import bytes_digest
from workflow.core.validation import NAME_RE, parse_header

from ..contracts import AgentEndpoint, AgentResult, TransportError, TransportRequest

ADAPTER_TYPE = "CLAUDE_CODE_LOCAL"

# Handoff Status -> transport classification. COMPLETE deliberately maps to
# READY_FOR_REVIEW: transport observes that a result exists, it never declares
# the work item complete. Only the workflow engine does that, from a validated
# handoff.
_STATUS_MAP = {
    "COMPLETE": "READY_FOR_REVIEW",
    "REQUEST_CHANGES": "READY_FOR_REVIEW",
    "REQUEST_WORKER": "READY_FOR_REVIEW",
    "BLOCKED": "BLOCKED",
    "NEEDS_DECISION": "NEEDS_DECISION",
}

# Host session context that must not leak into a spawned child. Inheriting these
# gives the child a different session's identity and routing.
_STRIPPED_ENV_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "CLAUDECODE", "CLAUDE_PID")


class ClaudeCodeAdapter:
    adapter_type = ADAPTER_TYPE

    def __init__(
        self,
        executable: Path,
        *,
        workspace: Path,
        result_dir: Path,
        runner=subprocess.run,
        agents_lister=None,
    ):
        executable = Path(executable)
        if not executable.is_absolute():
            raise TransportError("adapter-executable-must-be-absolute")
        self.executable = executable
        self.workspace = Path(workspace)
        self.result_dir = Path(result_dir)
        self._runner = runner
        self._agents_lister = agents_lister

    # -- process plumbing ------------------------------------------------

    @staticmethod
    def _child_env() -> Dict[str, str]:
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(_STRIPPED_ENV_PREFIXES):
                env.pop(key, None)
        return env

    def _build_argv(
        self,
        endpoint: AgentEndpoint,
        *,
        correlation_id: str,
        prompt: str,
    ) -> List[str]:
        """Assemble argv from the registered endpoint only.

        ``prompt`` is appended as a single list element. Because the process is
        spawned without a shell, its content -- however hostile -- is one opaque
        argument and can never become a flag or a second command.
        """
        capabilities = endpoint.capabilities
        argv: List[str] = [
            str(self.executable),
            "-p",
            "--session-id", correlation_id,
            "--output-format", "json",
            "--permission-mode", capabilities.permission_mode,
        ]
        if capabilities.model:
            argv += ["--model", capabilities.model]
        if capabilities.max_budget_usd > 0:
            argv += ["--max-budget-usd", str(capabilities.max_budget_usd)]
        if capabilities.tools:
            argv += ["--tools", ",".join(capabilities.tools)]
        else:
            argv += ["--tools", ""]
        for directory in capabilities.add_dirs:
            argv += ["--add-dir", directory]
        argv.append(prompt)
        return argv

    def _run(self, argv: List[str], timeout: float) -> Tuple[int, str, str]:
        completed = self._runner(
            argv,
            cwd=str(self.workspace),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=self._child_env(),
        )
        return completed.returncode, completed.stdout or "", completed.stderr or ""

    @staticmethod
    def _parse_envelope(stdout: str) -> Optional[Dict[str, Any]]:
        for line in reversed([l for l in stdout.splitlines() if l.strip()]):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("type") == "result":
                return value
        return None

    # -- LocalAdapter ----------------------------------------------------

    def start(
        self,
        request: TransportRequest,
        endpoint: AgentEndpoint,
        *,
        correlation_id: str,
        assignment: Dict[str, Any],
    ) -> AgentResult:
        prompt = str(assignment.get("prompt", "")).strip()
        if not prompt:
            return AgentResult(status="FAILED_FINAL", reason_code="assignment-prompt-empty", correlation_id=correlation_id)

        argv = self._build_argv(endpoint, correlation_id=correlation_id, prompt=prompt)
        try:
            code, stdout, stderr = self._run(argv, endpoint.capabilities.timeout_seconds)
        except subprocess.TimeoutExpired:
            return AgentResult(status="FAILED_RETRYABLE", reason_code="agent-timeout", correlation_id=correlation_id)
        except OSError as exc:
            return AgentResult(status="FAILED_FINAL", reason_code="agent-executable-not-runnable", detail=str(exc), correlation_id=correlation_id)

        envelope = self._parse_envelope(stdout)
        if envelope is None:
            # No parseable result envelope means we cannot say what happened.
            # Silence is not success.
            return AgentResult(
                status="FAILED_RETRYABLE",
                reason_code="agent-result-envelope-missing",
                detail=(stderr or stdout)[:500],
                correlation_id=correlation_id,
                evidence={"exit_code": code},
            )

        returned_session = str(envelope.get("session_id", ""))
        if returned_session != correlation_id:
            # A response from a different session cannot be trusted to belong to
            # this request.
            return AgentResult(
                status="FAILED_FINAL",
                reason_code="agent-session-identity-mismatch",
                detail=f"expected {correlation_id}, got {returned_session}",
                correlation_id=correlation_id,
            )

        evidence = {
            "exit_code": code,
            "terminal_reason": envelope.get("terminal_reason"),
            "subtype": envelope.get("subtype"),
            "num_turns": envelope.get("num_turns"),
            "permission_denials": envelope.get("permission_denials", []),
            "total_cost_usd": envelope.get("total_cost_usd"),
        }

        if envelope.get("is_error"):
            detail = str(envelope.get("result", ""))[:500]
            terminal_reason = str(envelope.get("terminal_reason", ""))
            retryable = terminal_reason in ("api_error", "overloaded", "")
            return AgentResult(
                status="FAILED_RETRYABLE" if retryable else "FAILED_FINAL",
                reason_code=f"agent-error:{terminal_reason or 'unknown'}",
                detail=detail,
                correlation_id=correlation_id,
                evidence=evidence,
            )

        # The run finished without error. Whether the work item actually advanced
        # is decided by collect(), from the produced handoff file -- not from the
        # model's prose.
        return AgentResult(status="WORKING", reason_code="agent-run-finished", correlation_id=correlation_id, evidence=evidence)

    def query(
        self,
        request: TransportRequest,
        endpoint: AgentEndpoint,
        *,
        correlation_id: str,
    ) -> AgentResult:
        sessions = self._list_sessions()
        for session in sessions:
            if str(session.get("sessionId", "")) == correlation_id:
                return AgentResult(
                    status="WORKING",
                    reason_code="agent-session-active",
                    correlation_id=correlation_id,
                    evidence={"pid": session.get("pid"), "kind": session.get("kind")},
                )

        # Not active. If a result is already on disk the task finished; otherwise
        # the session is gone without producing anything, which is a failure to
        # reconcile, never a success.
        candidates = self._result_candidates(request)
        if len(candidates) == 1:
            return AgentResult(status="READY_FOR_REVIEW", reason_code="agent-result-present", correlation_id=correlation_id, result_path=str(candidates[0]))
        if not candidates:
            return AgentResult(status="FAILED_RETRYABLE", reason_code="agent-session-not-found-no-result", correlation_id=correlation_id)
        return AgentResult(status="FAILED_FINAL", reason_code="agent-result-ambiguous", correlation_id=correlation_id, evidence={"candidate_count": len(candidates)})

    def _list_sessions(self) -> List[Dict[str, Any]]:
        if self._agents_lister is not None:
            return list(self._agents_lister())
        try:
            completed = self._runner(
                [str(self.executable), "agents", "--json", "--all"],
                text=True,
                capture_output=True,
                timeout=60,
                env=self._child_env(),
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        try:
            value = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

    def _result_candidates(self, request: TransportRequest) -> List[Path]:
        if not self.result_dir.is_dir():
            return []
        found = []
        for path in sorted(self.result_dir.iterdir()):
            if not path.is_file() or path.is_symlink():
                continue
            match = NAME_RE.match(path.name)
            if match and match.groupdict()["work"] == request.work_item:
                found.append(path)
        return found

    def collect(
        self,
        request: TransportRequest,
        endpoint: AgentEndpoint,
        *,
        correlation_id: str,
    ) -> AgentResult:
        candidates = self._result_candidates(request)
        if not candidates:
            return AgentResult(status="FAILED_RETRYABLE", reason_code="agent-result-missing", correlation_id=correlation_id)
        if len(candidates) > 1:
            # Ambiguity is a fail-closed condition: picking one would be Orbit
            # guessing which artifact is authoritative.
            return AgentResult(
                status="FAILED_FINAL",
                reason_code="agent-result-ambiguous",
                correlation_id=correlation_id,
                evidence={"candidates": [p.name for p in candidates]},
            )

        path = candidates[0]
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return AgentResult(status="FAILED_FINAL", reason_code="agent-result-unreadable", correlation_id=correlation_id)

        parsed = parse_header(text)
        if not parsed.ok:
            return AgentResult(status="FAILED_FINAL", reason_code=f"agent-result-{parsed.reason}", correlation_id=correlation_id)

        fields = parsed.fields
        if fields.get("work item") != request.work_item:
            return AgentResult(
                status="FAILED_FINAL",
                reason_code="agent-result-work-item-mismatch",
                detail=f"expected {request.work_item}, got {fields.get('work item')}",
                correlation_id=correlation_id,
            )

        status = str(fields.get("status", "")).upper()
        mapped = _STATUS_MAP.get(status)
        if mapped is None:
            return AgentResult(status="FAILED_FINAL", reason_code=f"agent-result-unsupported-status:{status or 'missing'}", correlation_id=correlation_id)

        return AgentResult(
            status=mapped,
            reason_code="agent-result-collected",
            correlation_id=correlation_id,
            result_path=str(path),
            artifact_digest=bytes_digest(data),
            evidence={"declared_status": status, "handoff_id": fields.get("handoff id")},
        )
