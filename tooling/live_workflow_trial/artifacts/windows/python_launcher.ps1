# Shared Python interpreter resolution for Orbit operator launchers.
#
# The launchers previously invoked "py -3" directly. The Windows Python launcher
# is an optional component: it is absent on hosts where Python comes from the
# Microsoft Store, and on python.org installs where the launcher was deselected.
# On such a host every Orbit launcher fails with CommandNotFoundException before
# reaching Python at all, which made native gate evidence silently depend on
# whichever interpreter shim happened to be on PATH during validation.
#
# This resolves an interpreter that can actually run Python 3 -- preferring the
# launcher when it works, falling back to python.exe on PATH -- and fails closed
# with a stable reason code when neither can.
#
# This adds no capability to Orbit. It only decides which already-installed
# interpreter the operator launchers invoke. It is not reachable from workflow
# runtime or handoff content, and it does not touch the executor catalog.

function Get-OrbitPython {
  <#
    .SYNOPSIS
      Returns the first interpreter able to run Python 3.
    .OUTPUTS
      Hashtable with Exe (executable name) and Prefix (leading argument array).
  #>
  $candidates = @(
    @{ Exe = "py";     Prefix = @("-3") },
    @{ Exe = "python"; Prefix = @() }
  )
  foreach ($candidate in $candidates) {
    if ($null -eq (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
    $probeArgs = @()
    $probeArgs += $candidate.Prefix
    $probeArgs += "--version"

    # Probe by actually running the interpreter. Presence on PATH is not enough:
    # "py" can be installed with no registered 3.x runtime, in which case it
    # resolves but cannot execute anything.
    $output = ""
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $output = (& $candidate.Exe @probeArgs 2>&1 | Out-String)
    } catch {
      $output = ""
    } finally {
      $ErrorActionPreference = $previous
    }

    if ($LASTEXITCODE -eq 0 -and $output -match "Python 3") {
      return $candidate
    }
  }
  throw "orbit-python-interpreter-not-found: no working Python 3 interpreter ('py -3' or 'python') is available on PATH"
}

function Invoke-OrbitPython {
  <#
    .SYNOPSIS
      Runs the resolved Python 3 interpreter with the supplied arguments.
    .NOTES
      $LASTEXITCODE is left set by the interpreter so callers keep their existing
      exit-code checks unchanged.
  #>
  param(
    [Parameter(Mandatory=$true)][string[]]$Arguments
  )
  $python = Get-OrbitPython
  $allArgs = @()
  $allArgs += $python.Prefix
  $allArgs += $Arguments

  # Windows PowerShell 5.1 turns a native command's redirected stderr into
  # ErrorRecords, which become terminating errors under ErrorActionPreference
  # 'Stop'. Python writes ordinary progress there (unittest -v, --version on some
  # builds), so stderr text must not be treated as failure. The process exit code
  # is the authority; callers check $LASTEXITCODE themselves.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $python.Exe @allArgs
  } finally {
    $ErrorActionPreference = $previous
  }
}
