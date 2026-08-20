"""Python side of the ChatGPT UIA driver.

Python passes an operation *name* from a fixed allowlist plus a JSON parameter
object. It never builds PowerShell source, so no caller-supplied string is ever
executed as script, and there is no operation that names a window, an
application, or a coordinate.

Every call is bounded by a timeout and returns a typed result. A driver that
crashes, times out, or emits garbage produces a denial, never an exception that
reaches workflow state.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

DRIVER = Path(__file__).with_name("uia_driver.ps1")

# The complete driver surface. Adding a row is an Architecture decision.
DRIVER_OPERATIONS = (
    "snapshot",
    "focus_chat",
    "active_chat",
    "set_message",
    "read_composer",
    "list_artifacts",
    "response_state",
    "press_send",
    "read_transcript_tail",
    "save_artifact_as",
)


class UiaResult(dict):
    """Thin dict wrapper so callers can read .ok without a second import."""

    @property
    def ok(self) -> bool:
        return bool(self.get("ok"))

    @property
    def reason_code(self) -> str:
        return str(self.get("reason_code", ""))

    @property
    def data(self) -> Dict[str, Any]:
        value = self.get("data")
        return value if isinstance(value, dict) else {}


def _deny(reason_code: str, detail: str = "") -> UiaResult:
    return UiaResult({"ok": False, "reason_code": reason_code, "detail": detail})


class UiaDriver:
    def __init__(self, *, driver_path: Optional[Path] = None, timeout: float = 60.0, runner=subprocess.run):
        self.driver_path = Path(driver_path) if driver_path else DRIVER
        self.timeout = float(timeout)
        self._runner = runner

    def call(self, operation: str, params: Optional[Dict[str, Any]] = None) -> UiaResult:
        if operation not in DRIVER_OPERATIONS:
            return _deny("driver-operation-not-allowlisted", operation)
        if not self.driver_path.is_file():
            return _deny("driver-script-missing", str(self.driver_path))

        argv = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(self.driver_path),
            "-Operation", operation,
            "-ParamsJson", json.dumps(params or {}),
        ]
        try:
            completed = self._runner(
                argv, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=self.timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return _deny("driver-timeout", operation)
        except OSError as exc:
            return _deny("driver-not-runnable", str(exc)[:200])

        stdout = (completed.stdout or "").strip()

        # Fast path: the compact JSON arrived on its own line.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return UiaResult(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # PowerShell's formatter wraps long output across lines, so a large
        # payload arrives as one JSON object split over many lines. Rejoin and
        # parse the outermost object rather than treating wrapping as failure.
        first, last = stdout.find("{"), stdout.rfind("}")
        if first != -1 and last > first:
            candidate = stdout[first:last + 1]
            for attempt in (candidate, candidate.replace("\r", "").replace("\n", "")):
                try:
                    return UiaResult(json.loads(attempt))
                except json.JSONDecodeError:
                    continue

        return _deny("driver-unparseable-output", ((completed.stderr or stdout) or "")[:300])

    # -- convenience wrappers -------------------------------------------

    def snapshot(self, chat_list_name: str = "") -> UiaResult:
        return self.call("snapshot", {"chat_list_name": chat_list_name})

    def focus_chat(self, *, chat_list_name: str, chat_title: str) -> UiaResult:
        return self.call("focus_chat", {"chat_list_name": chat_list_name, "chat_title": chat_title})

    def active_chat(self) -> UiaResult:
        return self.call("active_chat")

    def set_message(self, text: str) -> UiaResult:
        return self.call("set_message", {"text": text})

    def read_composer(self) -> UiaResult:
        return self.call("read_composer")

    def list_artifacts(self) -> UiaResult:
        return self.call("list_artifacts")

    def response_state(self) -> UiaResult:
        return self.call("response_state")

    def press_send(self) -> UiaResult:
        return self.call("press_send")

    def read_transcript_tail(self, max_chars: int = 6000) -> UiaResult:
        return self.call("read_transcript_tail", {"max_chars": max_chars})

    def save_artifact_as(self, *, filename: str, destination: str) -> UiaResult:
        return self.call("save_artifact_as", {"filename": filename, "destination": destination})
