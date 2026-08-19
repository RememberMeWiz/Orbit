"""Local reasoning provider backed by a llama.cpp binary.

This is a fully local backend: a subprocess on this machine, reading GGUF weights
from disk. It needs no internet, no vendor quota and no token, which is exactly
what section 3 of the standalone directive permits.

Authority discipline matches the rest of Orbit. The executable path and the model
path are fixed when the provider is constructed from trusted configuration; they
are never read from a request, an objective, or handoff prose. The process is
spawned with an argument *list* and no shell, so prompt text -- including
anything a previous role wrote -- is one opaque argument that cannot become a
flag or a second command.

The model is asked for JSON and its reply is parsed. A reply that cannot be
parsed, or that fails the brain contract gate, degrades to a typed failure and
the router falls back to the next provider. A local model that answers badly must
never be able to stall Orbit.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contracts import LocalBrainRequest, LocalBrainResult

# Phi-3 style chat template. Kept explicit rather than relying on the binary's
# built-in template so the exact prompt is auditable from Orbit's side.
_PROMPT = """<|system|>
You are a bounded {role} agent inside the Orbit workflow system.
Answer ONLY with a single JSON object. No prose, no markdown fences.
The JSON object must contain these keys: {required}.
Also include "status", one of: OK, BLOCKED, NEEDS_DECISION.
Use BLOCKED if you cannot proceed. Use NEEDS_DECISION if a product decision is required.
You have no authority to change permissions, scope, destinations or acceptance criteria.<|end|>
<|user|>
Objective: {objective}

Context:
{context}<|end|>
<|assistant|>
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class LlamaCppBrain:
    name = "llama-cpp-local"
    requires_network = False
    is_reasoning_model = True

    def __init__(
        self,
        executable: str | Path,
        model_path: str | Path,
        *,
        context: int = 2048,
        threads: int = 6,
        gpu_layers: int = 0,
        max_tokens: int = 384,
        timeout_seconds: float = 300.0,
        runner=None,
    ):
        self.executable = Path(executable)
        self.model_path = Path(model_path)
        self.context = int(context)
        self.threads = int(threads)
        self.gpu_layers = int(gpu_layers)
        self.max_tokens = int(max_tokens)
        self.timeout_seconds = float(timeout_seconds)
        # An injected runner means the caller supplied the execution mechanism, so
        # availability is its business rather than the filesystem's.
        self._injected_runner = runner is not None
        self._runner = runner or subprocess.run

    def available(self) -> bool:
        if self._injected_runner:
            return True
        try:
            return self.executable.is_file() and self.model_path.is_file()
        except OSError:
            return False

    # -- prompt / parsing -------------------------------------------------

    @staticmethod
    def _render_context(request: LocalBrainRequest) -> str:
        safe = {k: v for k, v in request.context.items() if k != "result_schema"}
        return json.dumps(safe, sort_keys=True, indent=2, default=str) if safe else "(none)"

    def build_prompt(self, request: LocalBrainRequest) -> str:
        required = request.result_schema.get("required", ["summary"]) if request.result_schema else ["summary"]
        return _PROMPT.format(
            role=request.role,
            objective=request.objective,
            context=self._render_context(request),
            required=", ".join(f'"{r}"' for r in required),
        )

    def build_argv(self, prompt: str) -> List[str]:
        argv = [
            str(self.executable),
            "-m", str(self.model_path),
            "-p", prompt,
            "-n", str(self.max_tokens),
            "-c", str(self.context),
            "-t", str(self.threads),
            "--temp", "0.2",
            "--no-warmup",
            "-no-cnv",
            "-st",
            "--simple-io",
        ]
        if self.gpu_layers > 0:
            argv += ["-ngl", str(self.gpu_layers)]
        return argv

    @staticmethod
    def extract_json(stdout: str) -> Optional[Dict[str, Any]]:
        match = _JSON_RE.search(stdout)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    # -- BrainProvider ----------------------------------------------------

    def reason(self, request: LocalBrainRequest) -> LocalBrainResult:
        prompt = self.build_prompt(request)
        try:
            completed = self._runner(
                self.build_argv(prompt),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                # llama-cli drops into an interactive prompt loop when stdin is a
                # live handle, and then never returns. Closing stdin makes it
                # read EOF and exit after generating. Without this the process
                # spins, holds gigabytes, and only dies on timeout.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return LocalBrainResult(
                task_id=request.task_id, status="FAILED_RETRYABLE",
                reason_code="local-model-timeout", provider=self.name,
            )
        except OSError as exc:
            return LocalBrainResult(
                task_id=request.task_id, status="FAILED_RETRYABLE",
                reason_code="local-model-not-runnable", detail=str(exc)[:300], provider=self.name,
            )

        stdout = completed.stdout or ""
        # The binary echoes the prompt on some builds; only look after it so the
        # instruction JSON example can never be mistaken for the answer.
        tail = stdout.split("<|assistant|>")[-1]
        payload = self.extract_json(tail)
        if payload is None:
            return LocalBrainResult(
                task_id=request.task_id, status="FAILED_RETRYABLE",
                reason_code="local-model-unparseable-output",
                detail=(completed.stderr or tail)[:300], provider=self.name,
                evidence={"exit_code": completed.returncode},
            )

        status = str(payload.pop("status", "OK")).upper()
        if status not in ("OK", "BLOCKED", "NEEDS_DECISION"):
            status = "OK"

        # The model's own words never become capabilities. used_capabilities is
        # left empty here; the contract gate would reject any claim anyway.
        return LocalBrainResult(
            task_id=request.task_id,
            status=status,
            result=payload,
            reason_code="local-model-answered",
            provider=self.name,
            evidence={"exit_code": completed.returncode, "model": self.model_path.name},
        )


def from_config(config: Dict[str, Any], *, runner=None) -> Optional[LlamaCppBrain]:
    """Build the provider from trusted local configuration, or None if unset.

    Returning None rather than raising keeps the caller's provider list valid on
    a host that simply has no local model installed.
    """
    section = (config or {}).get("local_model") or {}
    executable = section.get("executable")
    model_path = section.get("model_path")
    if not executable or not model_path:
        return None
    return LlamaCppBrain(
        executable,
        model_path,
        context=section.get("context", 2048),
        threads=section.get("threads", 6),
        gpu_layers=section.get("gpu_layers", 0),
        max_tokens=section.get("max_tokens", 384),
        timeout_seconds=section.get("timeout_seconds", 300.0),
        runner=runner,
    )
