"""Chat-app automation feasibility diagnostic.

Answers one question: does the installed chat app expose enough accessibility
surface for Orbit to drive it *semantically* -- by named controls rather than by
screen coordinates?

This exists because the answer is host-dependent and can change with an app
update. Rather than encoding "it didn't work in August" as folklore, the check
is runnable: if a future ChatGPT build turns its renderer accessibility on, this
reports FEASIBLE and the transport work becomes unblocked.

Read-only. It enumerates control structure, never message content, and never
touches credential or session state.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Controls Orbit would need to drive the apprenticeship loop semantically.
REQUIRED_CONTROLS = (
    "conversation_list",
    "chat_title",
    "message_composer",
    "attach_control",
    "send_control",
    "response_stream",
    "attachment_card",
)

# Below this, a tree is window chrome only (frame, panes, min/max/close).
CHROME_ONLY_THRESHOLD = 50

_PROBE_PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$ErrorActionPreference = "Stop"
$out = @{}
$p = Get-Process -Name "PROCESS_NAME" -ErrorAction SilentlyContinue |
     Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if (-not $p) {
  $out["app_running"] = $false
} else {
  $out["app_running"] = $true
  $out["pid"] = $p.Id
  $out["window_title"] = $p.MainWindowTitle
  $out["executable"] = $p.Path
  $root = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
  $out["class_name"] = $root.Current.ClassName
  $out["framework"] = $root.Current.FrameworkId
  $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                       [System.Windows.Automation.Condition]::TrueCondition)
  $out["uia_descendants"] = $all.Count
  $types = @{}
  foreach ($e in $all) {
    try {
      $t = $e.Current.ControlType.ProgrammaticName -replace "ControlType\.", ""
      if ($types.ContainsKey($t)) { $types[$t]++ } else { $types[$t] = 1 }
    } catch { }
  }
  $out["control_types"] = $types
  $edits = 0; $docs = 0
  foreach ($e in $all) {
    try {
      $t = $e.Current.ControlType.ProgrammaticName
      if ($t -eq "ControlType.Edit") { $edits++ }
      if ($t -eq "ControlType.Document") { $docs++ }
    } catch { }
  }
  $out["edit_controls"] = $edits
  $out["document_controls"] = $docs
}
$out | ConvertTo-Json -Depth 5 -Compress
"""


@dataclass
class FeasibilityReport:
    app_running: bool = False
    feasible: bool = False
    verdict: str = ""
    reason_code: str = ""
    observations: Dict[str, Any] = field(default_factory=dict)
    missing_controls: List[str] = field(default_factory=list)
    recommendation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_running": self.app_running,
            "feasible": self.feasible,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "observations": self.observations,
            "missing_controls": self.missing_controls,
            "recommendation": self.recommendation,
        }


def probe_uia(process_name: str = "ChatGPT", *, runner=subprocess.run, timeout: float = 120.0) -> Dict[str, Any]:
    """Run the read-only PowerShell UIA probe and return its parsed output."""
    if os.name != "nt":
        return {"app_running": False, "error": "not-windows"}
    script = _PROBE_PS.replace("PROCESS_NAME", process_name)
    try:
        completed = runner(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"app_running": False, "error": f"probe-failed:{type(exc).__name__}"}
    stdout = (completed.stdout or "").strip()
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"app_running": False, "error": "probe-unparseable", "detail": stdout[:300]}


def assess(observations: Dict[str, Any]) -> FeasibilityReport:
    """Turn raw probe output into a go / no-go verdict."""
    report = FeasibilityReport(observations=observations)

    if observations.get("error") == "not-windows":
        report.verdict = "NOT_APPLICABLE"
        report.reason_code = "not-windows"
        report.recommendation = "This diagnostic only applies to the Windows desktop app."
        return report

    if not observations.get("app_running"):
        report.verdict = "APP_NOT_RUNNING"
        report.reason_code = "chat-app-not-running"
        report.recommendation = "Start the chat app and re-run. Orbit must never search the desktop for it."
        return report

    report.app_running = True
    descendants = int(observations.get("uia_descendants", 0) or 0)
    edits = int(observations.get("edit_controls", 0) or 0)
    docs = int(observations.get("document_controls", 0) or 0)

    if descendants < CHROME_ONLY_THRESHOLD or (edits == 0 and docs == 0):
        # A Chromium shell with accessibility off looks exactly like this: a
        # window, a handful of panes, and the min/max/close buttons.
        report.feasible = False
        report.verdict = "NO_SEMANTIC_SURFACE"
        report.reason_code = "renderer-accessibility-inactive"
        report.missing_controls = list(REQUIRED_CONTROLS)
        report.recommendation = (
            "The app exposes window chrome only: no composer, send control, attachment card, "
            "conversation list or message stream. Every typed chat operation would require raw "
            "coordinate clicking, which cannot satisfy the endpoint-verification and "
            "attachment-confirmation gates. Do not build a coordinate bot. Re-run this after an "
            "app update, or after the app is started with renderer accessibility enabled."
        )
        return report

    report.feasible = True
    report.verdict = "SEMANTIC_SURFACE_PRESENT"
    report.reason_code = "uia-tree-populated"
    report.recommendation = (
        "An accessibility tree is present. Map the required controls to stable automation ids "
        "before implementing any send path, and keep coordinate fallbacks out of the typed surface."
    )
    return report


def run(process_name: str = "ChatGPT", *, runner=subprocess.run) -> FeasibilityReport:
    return assess(probe_uia(process_name, runner=runner))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    print(json.dumps(run().to_dict(), indent=2, sort_keys=True))
