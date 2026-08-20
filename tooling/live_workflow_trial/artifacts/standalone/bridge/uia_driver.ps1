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
# Conversation text is not ASCII. Emit UTF-8 so the JSON survives the pipe;
# the locale codepage mangles non-ASCII and produces invalid JSON.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

# Last-resort activation for controls this Electron app renders without a
# working programmatic activation path. Invoke succeeds but is inert, legacy
# DoDefaultAction is unsupported, and keyboard focus never lands on the menu
# entries -- all verified against the live app before adding this.
#
# The click point comes from the element's own UIA BoundingRectangle, never from
# a screen capture, a model, or handoff prose. Coordinates are an ephemeral
# actuator output computed at the moment of use and are never inputs, never
# stored, and never exposed through a typed operation. Every caller verifies the
# expected post-condition afterwards; a click that "worked" but changed nothing
# still fails.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class OrbitCursor {
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    public const uint LEFTDOWN = 0x0002, LEFTUP = 0x0004;
}
'@

# Declare DPI awareness before any geometry is read or any cursor is moved.
#
# This display runs at 125% scaling (1920x1080 physical, 1536x864 reported to a
# DPI-unaware process). UIA BoundingRectangle returns physical coordinates, but
# SetCursorPos from an unaware process is interpreted in the scaled space and
# multiplied by 1.25 -- so a click aimed at a menu item landed ~200px away,
# dismissing the menu without hitting anything. Becoming DPI-aware puts both
# APIs in the same coordinate space.
[void][OrbitCursor]::SetProcessDPIAware()

function Bring-ChatToFront([IntPtr]$hwnd) {
  # A coordinate click only makes sense when the target app is actually in
  # front. Windows refuses SetForegroundWindow from a background process unless
  # the calling thread is attached to the foreground thread's input queue, so
  # attach, raise, then detach. Bounded to the already-verified ChatGPT window.
  try {
    $fg = [OrbitCursor]::GetForegroundWindow()
    $fgThread = [OrbitCursor]::GetWindowThreadProcessId($fg, [ref]([int]0))
    $ourThread = [OrbitCursor]::GetCurrentThreadId()
    [void][OrbitCursor]::AttachThreadInput($fgThread, $ourThread, $true)
    [void][OrbitCursor]::ShowWindow($hwnd, 9)   # SW_RESTORE
    [void][OrbitCursor]::SetForegroundWindow($hwnd)
    [void][OrbitCursor]::AttachThreadInput($fgThread, $ourThread, $false)
    Start-Sleep -Milliseconds 500
    $now = [OrbitCursor]::GetForegroundWindow()
    return ($now -eq $hwnd)
  } catch { return $false }
}

# Keystrokes go to whatever window currently has focus, not to whatever element
# UIA last touched. SetFocus on a background window does NOT make that window
# foreground, so a Ctrl+A / Ctrl+V pair issued while ChatGPT is behind the
# operator's editor performs select-all-and-replace *in the editor*.
#
# So no keystroke is ever sent blind. The intended window must be verifiably in
# front at the moment of sending; if it cannot be raised, the operation fails
# and sends nothing. Refusing to type is always recoverable. Typing into the
# wrong window may not be.
function Send-KeysTo([IntPtr]$hwnd, [string]$keys) {
  if ($hwnd -eq [IntPtr]::Zero) { return $false }
  if ([OrbitCursor]::GetForegroundWindow() -ne $hwnd) {
    if (-not (Bring-ChatToFront $hwnd)) { return $false }
  }
  # Re-checked immediately before sending: the foreground can change between
  # the raise and the keystroke, and that gap is the whole hazard.
  if ([OrbitCursor]::GetForegroundWindow() -ne $hwnd) { return $false }
  try { [System.Windows.Forms.SendKeys]::SendWait($keys) } catch { return $false }
  return $true
}

function HandleOf($e) {
  try { return [IntPtr]([int]$e.Current.NativeWindowHandle) } catch { return [IntPtr]::Zero }
}

function Click-ElementGeometry($e, [int]$expectedPid) {
  try {
    # A coordinate click goes wherever the pointer is, so it is only safe when
    # the intended application is genuinely in front. If ChatGPT is not the
    # foreground window the click would land on whatever is -- the operator's
    # editor, a browser, anything. Refuse rather than click blind.
    $fg = [OrbitCursor]::GetForegroundWindow()
    $fgPid = 0
    [void][OrbitCursor]::GetWindowThreadProcessId($fg, [ref]$fgPid)
    if ($expectedPid -ne 0 -and $fgPid -ne $expectedPid) { return $false }

    $r = $e.Current.BoundingRectangle
    if ($r.Width -le 0 -or $r.Height -le 0) { return $false }
    $x = [int]($r.X + ($r.Width / 2))
    $y = [int]($r.Y + ($r.Height / 2))
    # Restore the pointer afterwards so the operator's cursor does not jump.
    $origin = New-Object OrbitCursor+POINT
    [void][OrbitCursor]::GetCursorPos([ref]$origin)
    [void][OrbitCursor]::SetCursorPos($x, $y)
    Start-Sleep -Milliseconds 120
    [OrbitCursor]::mouse_event([OrbitCursor]::LEFTDOWN, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 60
    [OrbitCursor]::mouse_event([OrbitCursor]::LEFTUP, 0, 0, 0, [IntPtr]::Zero)
    Start-Sleep -Milliseconds 250
    [void][OrbitCursor]::SetCursorPos($origin.X, $origin.Y)
    return $true
  } catch { return $false }
}


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

function Activate-Element($e) {
  # Controls in this app expose different patterns depending on how they are
  # rendered: web buttons carry Invoke, menu triggers carry ExpandCollapse, and
  # common-dialog controls are legacy windows carrying only LegacyIAccessible.
  # Try each rather than assuming one.
  try { $e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); return $true } catch { }
  try { $e.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern).DoDefaultAction(); return $true } catch { }
  try { $e.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand(); return $true } catch { }
  try { $e.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select(); return $true } catch { }
  return $false
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

    $composer = $null; $send = $null; $attach = $null; $doc = $null; $stop = $null
    $chatItems = @(); $headerChat = ""
    $counts = @{}

    foreach ($e in $all) {
      $t = CT $e; $n = NM $e; $c = ClassOf $e
      if ($counts.ContainsKey($t)) { $counts[$t]++ } else { $counts[$t] = 1 }
      if ($t -eq "Edit" -and $c -like "*ProseMirror*") { $composer = $n }
      if ($t -eq "Button" -and $n -ceq "Send") { $send = $n }
      # While a response streams, Send is replaced by Stop. Both are reported so
      # a caller can tell "this window is usable" apart from "this window is idle".
      if ($t -eq "Button" -and $n -ceq "Stop") { $stop = $n }
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
      stop_present = ($null -ne $stop)
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
    $hwnd = HandleOf $w
    $text = [string]$P.text
    if (-not $text) { Fail "message-empty" }

    $composer = $null
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Edit" -and (ClassOf $e) -like "*ProseMirror*") { $composer = $e; break }
    }
    if ($null -eq $composer) { Fail "composer-not-found" }

    $previousClipboard = $null
    try { $previousClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch { }

    $staged = $false
    try {
      Set-Clipboard -Value $text
      $composer.SetFocus()
      Start-Sleep -Milliseconds 250
      # Select-all then paste replaces any stale draft without pressing Enter.
      # Both are refused outright unless the chat window is genuinely in front,
      # because a select-all landing elsewhere would replace someone's work.
      if (Send-KeysTo $hwnd "^a") {
        Start-Sleep -Milliseconds 120
        $staged = Send-KeysTo $hwnd "^v"
        Start-Sleep -Milliseconds 450
      }
    } catch {
      Fail "composer-set-failed" $_.Exception.Message
    } finally {
      try {
        if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard }
        else { Set-Clipboard -Value " " }
      } catch { }
    }
    if (-not $staged) { Fail "composer-window-not-foreground" }
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

  # Read the tail of the currently open conversation so Orbit can find a PM
  # directive addressed to it. Scoped to the response Document of the verified
  # window: it reads the conversation Orbit already focused, nothing else, and
  # returns only a bounded tail rather than the whole history.
  "read_transcript_tail" {
    $w = Get-ChatWindow
    $maxChars = 6000
    if ($P.max_chars) { $maxChars = [int]$P.max_chars }

    $doc = $null
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Document") { $doc = $e; break }
    }
    if ($null -eq $doc) { Fail "response-document-not-found" }

    $parts = @()
    foreach ($e in $doc.FindAll($Scope::Descendants, $Cond::TrueCondition)) {
      if ((CT $e) -ne "Text") { continue }
      $n = ""
      try { $n = $e.Current.Name } catch { }
      if ($n) { $parts += $n }
    }
    $joined = ($parts -join "`n")
    $tail = if ($joined.Length -gt $maxChars) { $joined.Substring($joined.Length - $maxChars) } else { $joined }
    Done @{ text = $tail; total_length = $joined.Length; nodes = $parts.Count; truncated = ($joined.Length -gt $maxChars) }
  }

  # Materialise one artifact card to an exact path via the standard Windows
  # Save As dialog.
  #
  # The destination is written into the dialog's filename box, so the file lands
  # exactly where Orbit asked and never transits a shared Downloads folder. That
  # removes the "neighbouring file" hazard rather than merely mitigating it.
  #
  # Control shapes here are not the obvious ones: the common dialog exposes its
  # filename box and Save button as ControlType Pane with ClassName Edit/Button
  # (the legacy window wrapper), so matching on ControlType alone finds nothing.
  # Identity is (AutomationId + ClassName).
  #
  # FailDlg dismisses the dialog before exiting. Plain Fail calls exit, which
  # bypasses catch/finally, so a modal would otherwise be left on the operator's
  # screen.
  "save_artifact_as" {
    $w = Get-ChatWindow
    $filename = [string]$P.filename
    $destination = [string]$P.destination
    if (-not $filename -or -not $destination) { Fail "save-missing-params" }

    $cards = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -ne "Button") { continue }
      if ((NM $e) -like "Save $filename as*") { $cards += $e }
    }
    if ($cards.Count -eq 0) { Fail "artifact-card-not-found" $filename }
    if ($cards.Count -gt 1) { Fail "artifact-card-ambiguous" "$($cards.Count) cards for $filename" }

    $before = @{}
    foreach ($tw in $UIA::RootElement.FindAll($Scope::Children, $Cond::TrueCondition)) {
      try { $before["$($tw.Current.NativeWindowHandle)"] = $true } catch { }
    }

    if (-not (Invoke-Element $cards[0])) { Fail "artifact-card-not-invokable" }

    $dlg = $null
    for ($i = 0; $i -lt 15; $i++) {
      Start-Sleep -Milliseconds 700
      foreach ($tw in $UIA::RootElement.FindAll($Scope::Children, $Cond::TrueCondition)) {
        try {
          if ($before.ContainsKey("$($tw.Current.NativeWindowHandle)")) { continue }
          if ($tw.Current.ClassName -eq "#32770") { $dlg = $tw; break }
        } catch { }
      }
      if ($dlg) { break }
    }
    if ($null -eq $dlg) { Fail "save-dialog-did-not-appear" }
    $dlgHwnd = HandleOf $dlg

    function FailDlg([string]$code, [string]$detail = "") {
      try { $dlg.SetFocus(); Start-Sleep -Milliseconds 200 } catch { }
      # Escape is only pressed if the dialog is genuinely in front. If it is
      # not, the dialog is left open for a human rather than dismissing
      # whatever else is.
      try { [void](Send-KeysTo $dlgHwnd "{ESC}"); Start-Sleep -Milliseconds 600 } catch { }
      Fail $code $detail
    }

    $nameBox = $null; $saveBtn = $null
    foreach ($e in $dlg.FindAll($Scope::Descendants, $Cond::TrueCondition)) {
      try {
        $aid = $e.Current.AutomationId
        $cls = $e.Current.ClassName
        if (-not $nameBox -and $aid -eq "1001" -and $cls -eq "Edit") { $nameBox = $e }
        if (-not $saveBtn -and $aid -eq "1" -and $cls -eq "Button") { $saveBtn = $e }
      } catch { }
    }
    if ($null -eq $nameBox) { FailDlg "save-dialog-filename-box-not-found" }
    if ($null -eq $saveBtn) { FailDlg "save-dialog-save-button-not-found" }

    # The filename box is a legacy window wrapper and supports neither Value nor
    # Text patterns, so the path goes in by clipboard paste. Its Name property
    # does reflect the current contents, which is what makes verification
    # possible without a pattern.
    $previousClipboard = $null
    try { $previousClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch { }
    try {
      Set-Clipboard -Value $destination
      # Neither the filename box wrapper nor the dialog accepts a UIA SetFocus
      # call, and neither needs one: the dialog is modal and already owns
      # keyboard focus, with the filename box focused by default. The Name
      # readback below is what makes this safe -- if focus were anywhere else,
      # verification fails and nothing is committed.
      Start-Sleep -Milliseconds 350
      if (Send-KeysTo $dlgHwnd "^a") {
        Start-Sleep -Milliseconds 150
        $pathPasted = Send-KeysTo $dlgHwnd "^v"
        Start-Sleep -Milliseconds 500
      }
    } catch {
      try { if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard } } catch { }
      FailDlg "save-dialog-path-not-writable" $_.Exception.Message
    } finally {
      try { if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard } } catch { }
    }

    # Confirm the box holds exactly what Orbit asked for before committing.
    # Poll: the Name property updates asynchronously after a paste.
    $confirmed = ""
    for ($i = 0; $i -lt 8; $i++) {
      $confirmed = NM $nameBox
      if ($confirmed -ceq $destination) { break }
      Start-Sleep -Milliseconds 350
    }
    if ($confirmed -cne $destination) {
      FailDlg "save-dialog-path-not-accepted" "wrote '$destination', box holds '$confirmed'"
    }

    # The Save button is a legacy window wrapper with no Invoke pattern. Commit
    # with Enter instead, which is the dialog's own default action.
    #
    # This only runs after the filename box has been read back and matched
    # exactly, so what Enter commits is already known -- the button's presence
    # above is a precondition proving this is a real Save As dialog, not the
    # mechanism.
    $committed = $false
    try {
      $legacy = $saveBtn.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
      $legacy.DoDefaultAction()
      $committed = $true
    } catch { }
    if (-not $committed) {
      # Enter commits. It is only pressed with the dialog verifiably in front,
      # since the same keystroke sent anywhere else activates that window's
      # default action instead.
      if (-not (Send-KeysTo $dlgHwnd "{ENTER}")) { FailDlg "save-dialog-not-foreground" }
    }

    # The dialog closing is the app's own signal that it accepted the path.
    $closed = $false
    for ($i = 0; $i -lt 20; $i++) {
      Start-Sleep -Milliseconds 600
      $stillThere = $false
      foreach ($tw in $UIA::RootElement.FindAll($Scope::Children, $Cond::TrueCondition)) {
        try { if ($tw.Current.ClassName -eq "#32770" -and $tw.Current.Name -eq "Save As") { $stillThere = $true; break } } catch { }
      }
      if (-not $stillThere) { $closed = $true; break }
    }
    if (-not $closed) { FailDlg "save-dialog-did-not-close" }

    Done @{ filename = $filename; destination = $destination; confirmed_path = $confirmed }
  }

  # Report which files are currently staged on the composer.
  "attachment_state" {
    $w = Get-ChatWindow
    $names = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -ne "Button") { continue }
      $n = NM $e
      # Staged files render with a remove affordance; that is what distinguishes
      # an attachment chip from the same filename appearing in the transcript.
      if ($n -like "Remove *") { $names += ($n -replace "^Remove ", "") }
    }
    Done @{ attached = $names; count = $names.Count }
  }

  # Attach one local file to the composer of the already-focused conversation.
  #
  # This does NOT drive the "Add files and more" menu. That path was implemented
  # and abandoned after testing: the menu opens, but its "Add photos & files"
  # entry cannot be activated. UIA Invoke reports success and does nothing,
  # LegacyIAccessible is unsupported, ExpandCollapse is a no-op, keyboard focus
  # never lands on menu entries, and a click on the element's own reported
  # rectangle dismisses the menu without hitting the item. See the burst handoff.
  #
  # Instead the file is placed on the clipboard as a file-drop list and pasted
  # into the composer, which is the same mechanism a person uses with Ctrl+V.
  # It needs no menu, no file dialog, and no coordinates at all -- so it removes
  # the GUI fragility rather than working around it.
  #
  # Nothing is sent: staging and sending stay separate so the attachment can be
  # verified in the UI first.
  "attach_file" {
    $w = Get-ChatWindow
    $hwnd = HandleOf $w
    $path = [string]$P.path
    if (-not $path) { Fail "attach-missing-path" }
    if (-not (Test-Path -LiteralPath $path)) { Fail "attach-file-not-found" $path }
    $full = (Resolve-Path -LiteralPath $path).Path
    $leaf = Split-Path -Leaf $full

    $composer = $null
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Edit" -and (ClassOf $e) -like "*ProseMirror*") { $composer = $e; break }
    }
    if ($null -eq $composer) { Fail "composer-not-found" }

    # Clipboard is global state; save and restore the text contents around this.
    $previousClipboard = $null
    try { $previousClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch { }

    try {
      $files = New-Object System.Collections.Specialized.StringCollection
      [void]$files.Add($full)
      [System.Windows.Forms.Clipboard]::SetFileDropList($files)
      Start-Sleep -Milliseconds 400

      $composer.SetFocus()
      Start-Sleep -Milliseconds 300
      $pasted = Send-KeysTo $hwnd "^v"
      Start-Sleep -Milliseconds 1500
      if (-not $pasted) {
        try { if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard } } catch { }
        Fail "attach-window-not-foreground"
      }
    } catch {
      try { if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard } } catch { }
      Fail "attach-clipboard-paste-failed" $_.Exception.Message
    } finally {
      try { if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard } } catch { }
    }

    # Post-condition: the app must show the file staged, by its own remove
    # affordance. A paste that appeared to work but staged nothing still fails.
    $staged = @()
    for ($i = 0; $i -lt 15; $i++) {
      Start-Sleep -Milliseconds 700
      $staged = @()
      foreach ($e in (All-Descendants $w)) {
        if ((CT $e) -ne "Button") { continue }
        $n = NM $e
        if ($n -like "Remove *") { $staged += ($n -replace "^Remove ", "") }
      }
      if ($staged -contains $leaf) { break }
    }
    if ($staged -notcontains $leaf) {
      Fail "attach-not-staged" "expected '$leaf', staged: $($staged -join ', ')"
    }

    Done @{ path = $full; filename = $leaf; staged = $staged; activation = "clipboard-file-drop" }
  }

  # Remove staged attachments from the composer. Used to leave the app clean
  # when a dispatch is abandoned, so an unsent file is never left lying in a
  # real conversation.
  "clear_attachments" {
    $w = Get-ChatWindow
    $removed = @()
    for ($pass = 0; $pass -lt 10; $pass++) {
      $btn = $null
      foreach ($e in (All-Descendants $w)) {
        if ((CT $e) -eq "Button" -and (NM $e) -like "Remove *") { $btn = $e; break }
      }
      if ($null -eq $btn) { break }
      $name = (NM $btn) -replace "^Remove ", ""
      if (-not (Activate-Element $btn)) { break }
      $removed += $name
      Start-Sleep -Milliseconds 700
    }
    $left = @()
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Button" -and (NM $e) -like "Remove *") { $left += ((NM $e) -replace "^Remove ", "") }
    }
    Done @{ removed = $removed; remaining = $left }
  }

  # Read-only introspection of one named control. Used to discover which
  # activation pattern a control actually supports instead of assuming, since
  # this app renders otherwise-identical controls with different patterns.
  "describe_control" {
    $w = Get-ChatWindow
    $wanted = [string]$P.name
    if (-not $wanted) { Fail "describe-missing-name" }
    $hits = @()
    foreach ($e in (All-Descendants $w)) {
      $n = NM $e
      if ($n -cne $wanted) { continue }
      $pats = @()
      try { foreach ($pat in $e.GetSupportedPatterns()) { $pats += ($pat.ProgrammaticName -replace "PatternIdentifiers\.Pattern", "") } } catch { }
      $hits += @{
        control_type = (CT $e)
        class_name = (ClassOf $e)
        automation_id = $e.Current.AutomationId
        enabled = $e.Current.IsEnabled
        keyboard_focusable = $e.Current.IsKeyboardFocusable
        offscreen = $e.Current.IsOffscreen
        patterns = $pats
      }
    }
    Done @{ name = $wanted; matches = $hits; count = $hits.Count }
  }

  # Report whether the app is running, whether it was started with renderer
  # accessibility, and whether the semantic tree is actually usable. Unlike
  # every other operation this one must succeed when the app is absent -- "not
  # running" is an answer, not a failure.
  "app_state" {
    $procs = @(Get-Process -Name ChatGPT -ErrorAction SilentlyContinue)
    $windowed = @($procs | Where-Object { $_.MainWindowHandle -ne 0 })

    if (-not $procs) {
      Done @{
        running = $false; windowed = $false; trusted_path = $false
        accessibility_flag = $false; accessibility_ready = $false
        executable = ""; reason = "not-running"
      }
    }

    $proc = if ($windowed) { $windowed | Select-Object -First 1 } else { $procs | Select-Object -First 1 }
    $path = ""
    try { $path = [string]$proc.Path } catch { }
    $trusted = ($path -like "*OpenAI.Codex*")

    # The flag is only observable on the command line. A renderer that was
    # started without it exposes no semantic tree no matter how long we wait.
    $cmdline = ""
    try { $cmdline = [string](Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.Id)" -ErrorAction Stop).CommandLine } catch { }
    $flagged = ($cmdline -like "*--force-renderer-accessibility*")

    # Whether the flag took effect is a separate question from whether it was
    # passed, so it is measured rather than inferred: a composer in the tree is
    # the smallest proof that renderer accessibility is live.
    $ready = $false; $descendants = 0
    if ($windowed -and $trusted) {
      try {
        $el = $UIA::FromHandle($proc.MainWindowHandle)
        if ($null -ne $el) {
          $all = All-Descendants $el
          $descendants = $all.Count
          foreach ($e in $all) {
            if ((CT $e) -eq "Edit" -and (ClassOf $e) -like "*ProseMirror*") { $ready = $true; break }
          }
        }
      } catch { }
    }

    Done @{
      running = $true
      windowed = [bool]$windowed
      trusted_path = $trusted
      accessibility_flag = $flagged
      accessibility_ready = $ready
      descendants = $descendants
      executable = $path
      process_count = $procs.Count
      reason = "observed"
    }
  }

  # Start the app with renderer accessibility enabled.
  #
  # Refuses outright if any ChatGPT process already exists. Orbit does not get
  # to end a session a human may be using, and an app already running without
  # the flag can only be fixed by that human restarting it -- so this reports
  # the problem instead of "fixing" it.
  "launch_app" {
    $existing = @(Get-Process -Name ChatGPT -ErrorAction SilentlyContinue)
    if ($existing) { Fail "launch-refused-already-running" "$($existing.Count) process(es)" }

    # Resolved from the installed package rather than a pinned path, so a normal
    # app update does not silently disarm the guard.
    $pkg = Get-AppxPackage -Name "OpenAI.Codex" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pkg) { Fail "launch-package-not-installed" }
    $exe = Join-Path $pkg.InstallLocation "app\ChatGPT.exe"
    if (-not (Test-Path -LiteralPath $exe)) { Fail "launch-executable-missing" $exe }
    if ($exe -notlike "*OpenAI.Codex*") { Fail "launch-untrusted-path" $exe }

    try { Start-Process -FilePath $exe -ArgumentList "--force-renderer-accessibility" | Out-Null }
    catch { Fail "launch-failed" $_.Exception.Message }

    # Report what actually came up. A launch that starts a process but never
    # produces a usable window is a failure the caller needs to see.
    $waitSeconds = 60.0
    if ($P.timeout_seconds) { $waitSeconds = [double]$P.timeout_seconds }
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    $seen = $false
    while ((Get-Date) -lt $deadline) {
      $p = @(Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 })
      if ($p) { $seen = $true; break }
      Start-Sleep -Milliseconds 500
    }
    if (-not $seen) { Fail "launch-no-window" $exe }
    Done @{ launched = $true; executable = $exe; package_version = [string]$pkg.Version }
  }

  default { Fail "operation-not-supported" $Operation }
}
