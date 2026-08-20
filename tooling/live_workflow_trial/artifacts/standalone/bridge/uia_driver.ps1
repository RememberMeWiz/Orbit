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
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
    public const uint LEFTDOWN = 0x0002, LEFTUP = 0x0004;
}
'@

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

    function FailDlg([string]$code, [string]$detail = "") {
      try { $dlg.SetFocus(); Start-Sleep -Milliseconds 200 } catch { }
      try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}"); Start-Sleep -Milliseconds 600 } catch { }
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
      [System.Windows.Forms.SendKeys]::SendWait("^a")
      Start-Sleep -Milliseconds 150
      [System.Windows.Forms.SendKeys]::SendWait("^v")
      Start-Sleep -Milliseconds 500
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
      try { [System.Windows.Forms.SendKeys]::SendWait("{ENTER}") } catch { FailDlg "save-dialog-commit-failed" $_.Exception.Message }
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
  # The path is supplied by Orbit and pasted into the file dialog, so the
  # selection is exact rather than a click on whatever the picker happened to
  # highlight. Nothing is sent: staging and sending are separate operations so
  # the attachment can be verified in the UI first.
  "attach_file" {
    $w = Get-ChatWindow
    $path = [string]$P.path
    if (-not $path) { Fail "attach-missing-path" }
    if (-not (Test-Path -LiteralPath $path)) { Fail "attach-file-not-found" $path }
    $full = (Resolve-Path -LiteralPath $path).Path

    $attach = $null
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Button" -and (NM $e) -ceq "Add files and more") { $attach = $e; break }
    }
    if ($null -eq $attach) { Fail "attach-control-not-found" }

    $before = @{}
    foreach ($tw in $UIA::RootElement.FindAll($Scope::Children, $Cond::TrueCondition)) {
      try { $before["$($tw.Current.NativeWindowHandle)"] = $true } catch { }
    }

    if (-not (Activate-Element $attach)) { Fail "attach-control-not-activatable" }
    Start-Sleep -Milliseconds 1200

    $picker = $null
    foreach ($e in (All-Descendants $w)) {
      if ((CT $e) -eq "Button" -and (NM $e) -ceq "Add photos & files") { $picker = $e; break }
    }
    if ($null -eq $picker) {
      try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch { }
      Fail "attach-menu-entry-not-found"
    }

    # Activation path for this entry, established by testing against the live
    # app rather than assumed:
    #   UIA Invoke                -> reports success, does nothing (Electron)
    #   LegacyIAccessible         -> unsupported
    #   ExpandCollapse            -> no-op
    #   keyboard focus navigation -> focus never lands on menu entries
    #
    # So the click point is taken from the element's own UIA BoundingRectangle.
    # It is an ephemeral actuator output computed at the moment of use: never an
    # input, never stored, never reachable from a typed operation or from prose.
    # The dialog check below is the post-condition -- a click that "worked" but
    # opened nothing still fails.
    $chatPid = 0
    try { $chatPid = (Get-Process -Name ChatGPT | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1).Id } catch { }
    if (-not (Click-ElementGeometry $picker $chatPid)) {
      try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch { }
      Fail "attach-menu-entry-not-activatable" "no semantic activation path; geometry click refused or failed (ChatGPT must be the foreground window)"
    }
    $activation = "geometry-fallback"

    # A first-time file dialog can be slow to construct, so wait generously
    # rather than reporting a false negative. Record what did appear so a
    # failure says something useful instead of just "nothing".
    $dlg = $null
    $seen = @()
    for ($i = 0; $i -lt 40; $i++) {
      Start-Sleep -Milliseconds 700
      foreach ($tw in $UIA::RootElement.FindAll($Scope::Children, $Cond::TrueCondition)) {
        try {
          if ($before.ContainsKey("$($tw.Current.NativeWindowHandle)")) { continue }
          $seen += "$($tw.Current.ClassName)|$($tw.Current.Name)"
          if ($tw.Current.ClassName -eq "#32770") { $dlg = $tw; break }
        } catch { }
      }
      if ($dlg) { break }
    }
    if ($null -eq $dlg) {
      try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}") } catch { }
      Fail "attach-dialog-did-not-appear" (($seen | Select-Object -Unique -First 6) -join " ; ")
    }

    function FailPick([string]$code, [string]$detail = "") {
      try { [System.Windows.Forms.SendKeys]::SendWait("{ESC}"); Start-Sleep -Milliseconds 600 } catch { }
      Fail $code $detail
    }

    $nameBox = $null; $openBtn = $null
    foreach ($e in $dlg.FindAll($Scope::Descendants, $Cond::TrueCondition)) {
      try {
        $aid = $e.Current.AutomationId; $cls = $e.Current.ClassName
        if (-not $nameBox -and $aid -eq "1148" -and $cls -eq "Edit") { $nameBox = $e }
        if (-not $nameBox -and $aid -eq "1001" -and $cls -eq "Edit") { $nameBox = $e }
        if (-not $openBtn -and $aid -eq "1" -and $cls -eq "Button") { $openBtn = $e }
      } catch { }
    }
    if ($null -eq $nameBox) { FailPick "attach-dialog-filename-box-not-found" }

    $previousClipboard = $null
    try { $previousClipboard = Get-Clipboard -Raw -ErrorAction SilentlyContinue } catch { }
    try {
      Set-Clipboard -Value $full
      Start-Sleep -Milliseconds 350
      [System.Windows.Forms.SendKeys]::SendWait("^a")
      Start-Sleep -Milliseconds 150
      [System.Windows.Forms.SendKeys]::SendWait("^v")
      Start-Sleep -Milliseconds 500
    } catch {
      FailPick "attach-dialog-path-not-writable" $_.Exception.Message
    } finally {
      try { if ($null -ne $previousClipboard) { Set-Clipboard -Value $previousClipboard } } catch { }
    }

    $confirmed = ""
    for ($i = 0; $i -lt 8; $i++) {
      $confirmed = NM $nameBox
      if ($confirmed -ceq $full) { break }
      Start-Sleep -Milliseconds 350
    }
    if ($confirmed -cne $full) { FailPick "attach-dialog-path-not-accepted" "wrote '$full', box holds '$confirmed'" }

    $committed = $false
    if ($openBtn) { $committed = Activate-Element $openBtn }
    if (-not $committed) {
      try { [System.Windows.Forms.SendKeys]::SendWait("{ENTER}") } catch { FailPick "attach-commit-failed" }
    }

    $closed = $false
    for ($i = 0; $i -lt 20; $i++) {
      Start-Sleep -Milliseconds 600
      $stillThere = $false
      foreach ($tw in $UIA::RootElement.FindAll($Scope::Children, $Cond::TrueCondition)) {
        try { if ($tw.Current.ClassName -eq "#32770" -and -not $before.ContainsKey("$($tw.Current.NativeWindowHandle)")) { $stillThere = $true; break } } catch { }
      }
      if (-not $stillThere) { $closed = $true; break }
    }
    if (-not $closed) { FailPick "attach-dialog-did-not-close" }

    $leaf = Split-Path -Leaf $full
    Done @{ path = $full; filename = $leaf; activation = $activation }
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

  default { Fail "operation-not-supported" $Operation }
}
