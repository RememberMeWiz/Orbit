# Orbit ChatGPT UIA driver.
#
# One bounded operation per invocation, JSON in / JSON out. Python never builds
# PowerShell source: it passes an operation name and a parameter object, so no
# caller-supplied string is ever executed as script.
#
# Every operation is scoped to the verified ChatGPT window. There is no
# "click at x,y", no "type keys into whatever is focused", and no way to name a
# different application.

param(
  [Parameter(Mandatory=$true)][string]$Operation,
  [string]$ParamsJson = "{}"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

$P = $ParamsJson | ConvertFrom-Json
$UIA = [System.Windows.Automation.AutomationElement]
$Cond = [System.Windows.Automation.Condition]
$Scope = [System.Windows.Automation.TreeScope]

function Fail([string]$code, [string]$detail = "") {
  @{ ok = $false; reason_code = $code; detail = $detail } | ConvertTo-Json -Depth 6 -Compress
  exit 0
}
function Done($data) {
  $out = @{ ok = $true; reason_code = "ok" }
  if ($data) { $out["data"] = $data }
  $out | ConvertTo-Json -Depth 8 -Compress
  exit 0
}

function Get-ChatWindow {
  $procs = Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }
  if (-not $procs) { Fail "chat-app-not-running" }
  $proc = $procs | Select-Object -First 1
  # Only the trusted installed package may be driven.
  if ($proc.Path -and $proc.Path -notlike "*OpenAI.Codex*") { Fail "chat-app-untrusted-path" $proc.Path }
  $el = $UIA::FromHandle($proc.MainWindowHandle)
  if ($null -eq $el) { Fail "chat-window-unavailable" }
  return $el
}

function All-Descendants($root) {
  return $root.FindAll($Scope::Descendants, $Cond::TrueCondition)
}

function CT($e) {
  try { return $e.Current.ControlType.ProgrammaticName -replace "ControlType\.", "" } catch { return "" }
}
function NM($e) { try { return ($e.Current.Name -replace "\s+", " ").Trim() } catch { return "" } }
# NOTE: composer ClassName is matched by substring, not equality. ProseMirror
# appends state classes ("ProseMirror ProseMirror-focused") once the editor
# has keyboard focus, so exact equality breaks the moment Orbit focuses it.
#
# NOTE: do not name this CLS -- that is a built-in alias for Clear-Host, and
# PowerShell resolves aliases before functions, so the call silently returns
# $null and every ClassName comparison quietly fails.
function ClassOf($e) { try { return $e.Current.ClassName } catch { return "" } }

function Find-ByTypeName($root, [string]$type, [string]$name, [switch]$Exact) {
  $hits = @()
  foreach ($e in (All-Descendants $root)) {
    if ((CT $e) -ne $type) { continue }
    $n = NM $e
    if ($Exact) { if ($n -ceq $name) { $hits += $e } }
    else { if ($n -like "*$name*") { $hits += $e } }
  }
  return $hits
}

function Invoke-Element($e) {
  try {
    $pattern = $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
    $pattern.Invoke()
    return $true
  } catch { }
  try {
    $sel = $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $sel.Select()
    return $true
  } catch { }
  return $false
}

switch ($Operation) {

  # Structural snapshot used for endpoint verification and readiness checks.
  "snapshot" {
    $w = Get-ChatWindow
    $all = All-Descendants $w
    $listName = if ($P.chat_list_name) { $P.chat_list_name } else { "" }

    $composer = $null; $send = $null; $attach = $null; $doc = $null
    $chatItems = @(); $headerChat = ""
    $counts = @{}

    foreach ($e in $all) {
      $t = CT $e; $n = NM $e; $c = ClassOf $e
      if ($counts.ContainsKey($t)) { $counts[$t]++ } else { $counts[$t] = 1 }
      if ($t -eq "Edit" -and $c -like "*ProseMirror*") { $composer = $n }
      if ($t -eq "Button" -and $n -ceq "Send") { $send = $n }
      if ($t -eq "Button" -and $n -ceq "Add files and more") { $attach = $n }
      if ($t -eq "Document") { $doc = $n }
    }

    # Conversation titles are read ONLY from inside the configured project list,
    # so the active-chat header (which repeats a chat name) cannot create a
    # false ambiguity, and chats from other projects cannot be addressed.
    if ($listName) {
      foreach ($e in $all) {
        if ((CT $e) -ne "List") { continue }
        if ((NM $e) -cne $listName) { continue }
        foreach ($child in $e.FindAll($Scope::Descendants, $Cond::TrueCondition)) {
          if ((CT $child) -eq "ListItem") {
            $cn = NM $child
            if ($cn) { $chatItems += $cn }
          }
        }
      }
    }

    Done @{
      descendants = $all.Count
      control_types = $counts
      composer_present = ($null -ne $composer)
      composer_name = $composer
      send_present = ($null -ne $send)
      attach_present = ($null -ne $attach)
      document_name = $doc
      chat_list_name = $listName
      chat_items = $chatItems
    }
  }

  # Select a conversation by exact title, scoped to the configured project list.
  "focus_chat" {
    $w = Get-ChatWindow
    $listName = [string]$P.chat_list_name
    $title = [string]$P.chat_title
    if (-not $listName -or -not $title) { Fail "focus-missing-params" }

    $lists = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "List" -and (NM $e) -ceq $listName) { $lists += $e }
    }
    if ($lists.Count -eq 0) { Fail "chat-list-not-found" $listName }
    if ($lists.Count -gt 1) { Fail "chat-list-ambiguous" "$($lists.Count) lists named $listName" }

    $matches = @()
    foreach ($child in $lists[0].FindAll($Scope::Descendants, $Cond::TrueCondition)) {
      $t = CT $child
      if ($t -ne "ListItem" -and $t -ne "Button") { continue }
      if ((NM $child) -ceq $title) { $matches += $child }
    }
    if ($matches.Count -eq 0) { Fail "chat-not-observed" $title }

    # Exactly one actionable target, or refuse.
    $invokable = @()
    foreach ($m in $matches) {
      try { $null = $m.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern); $invokable += $m } catch { }
    }
    $target = if ($invokable.Count -ge 1) { $invokable[0] } else { $matches[0] }
    if ($invokable.Count -gt 1) { Fail "chat-ambiguous-observed" "$($invokable.Count) invokable matches for $title" }

    if (-not (Invoke-Element $target)) { Fail "chat-not-invokable" $title }
    Start-Sleep -Milliseconds 900
    Done @{ focused_title = $title }
  }

  # Which conversation is currently open, per the header region.
  "active_chat" {
    $w = Get-ChatWindow
    $names = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Button" -and (NM $e) -like "Project:*") { $names += (NM $e) }
    }
    $titleBtn = ""
    $seenProject = $false
    foreach ($e in (All-Descendants $w)) {
      $t = CT $e; $n = NM $e
      if ($t -eq "Button" -and $n -like "Project:*") { $seenProject = $true; continue }
      if ($seenProject -and $t -eq "Button" -and $n) { $titleBtn = $n; break }
    }
    Done @{ project_markers = $names; active_chat_title = $titleBtn }
  }

  # Stage a bounded message into the composer via clipboard paste.
  #
  # NEVER type this with SendKeys. A newline sent as a keystroke is an Enter
  # press, and Enter submits in this app -- typing a multi-line message
  # transmits it line by line during staging, before any verification gate can
  # run. Pasting inserts the text as literal content and sends nothing.
  #
  # The clipboard is global state, so the previous contents are saved and
  # restored around the paste.
  "set_message" {
    $w = Get-ChatWindow
    $text = [string]$P.text
    if (-not $text) { Fail "message-empty" }

    $composer = $null
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Edit" -and (ClassOf $e) -like "*ProseMirror*") { $composer = $e; break }
    }
    if ($null -eq $composer) { Fail "composer-not-found" }

    $previousClipboard = $null
    try { $previousClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch { }

    try {
      Set-Clipboard -Value $text
      $composer.SetFocus()
      Start-Sleep -Milliseconds 250
      # Select-all then paste replaces any stale draft without pressing Enter.
      [System.Windows.Forms.SendKeys]::SendWait("^a")
      Start-Sleep -Milliseconds 120
      [System.Windows.Forms.SendKeys]::SendWait("^v")
      Start-Sleep -Milliseconds 450
    } catch {
      Fail "composer-set-failed" $_.Exception.Message
    } finally {
      try {
        if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard }
        else { Set-Clipboard -Value " " }
      } catch { }
    }
    Done @{ length = $text.Length; method = "clipboard-paste" }
  }

  # Read the composer contents back so the caller can verify exactly what is
  # staged before anything is transmitted. The Name property is only the
  # placeholder, so the actual text comes from TextPattern/ValuePattern.
  "read_composer" {
    $w = Get-ChatWindow
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Edit" -and (ClassOf $e) -like "*ProseMirror*") {
        $text = ""
        $source = "none"
        try {
          $tp = $e.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
          $text = $tp.DocumentRange.GetText(20000)
          $source = "TextPattern"
        } catch {
          try {
            $vp = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $text = $vp.Current.Value
            $source = "ValuePattern"
          } catch { }
        }
        Done @{ name = (NM $e); text = $text; source = $source; length = $text.Length }
      }
    }
    Fail "composer-not-found"
  }

  # Enumerate downloadable artifact cards in the transcript.
  "list_artifacts" {
    $w = Get-ChatWindow
    $saves = @(); $previews = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -ne "Button") { continue }
      $n = NM $e
      if ($n -like "Save * as*") { $saves += ($n -replace "^Save ", "" -replace " as.*$", "") }
      elseif ($n -like "Open preview of *") { $previews += ($n -replace "^Open preview of ", "") }
    }
    Done @{ saveable = $saves; previewable = $previews }
  }

  # Is the assistant still generating?
  "response_state" {
    $w = Get-ChatWindow
    $hasSend = $false; $hasStop = $false; $names = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -ne "Button") { continue }
      $n = NM $e
      if ($n -ceq "Send") { $hasSend = $true }
      if ($n -match "(?i)^(stop|stop generating|stop streaming)$") { $hasStop = $true; $names += $n }
    }
    $state = if ($hasStop) { "streaming" } elseif ($hasSend) { "idle" } else { "unknown" }
    Done @{ state = $state; send_present = $hasSend; stop_present = $hasStop; stop_names = $names }
  }

  # The only operation that actually transmits. Requires exactly one enabled
  # Send button, and refuses while a response is streaming.
  "press_send" {
    $w = Get-ChatWindow
    $sends = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Button" -and (NM $e) -ceq "Send") { $sends += $e }
    }
    if ($sends.Count -eq 0) { Fail "send-control-not-found" }
    if ($sends.Count -gt 1) { Fail "send-control-ambiguous" "$($sends.Count) Send buttons" }
    $send = $sends[0]
    if (-not $send.Current.IsEnabled) { Fail "send-control-disabled" }

    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Button" -and (NM $e) -match "(?i)^(stop|stop generating|stop streaming)$") {
        Fail "response-in-progress"
      }
    }

    if (-not (Invoke-Element $send)) { Fail "send-not-invokable" }
    Start-Sleep -Milliseconds 700
    Done @{ sent = $true }
  }

  default { Fail "operation-not-supported" $Operation }
}
