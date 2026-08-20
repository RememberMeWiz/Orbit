param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$OperatorArgs
)

$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot
$ArtifactsRoot = Join-Path $RepoRoot "tooling\live_workflow_trial\artifacts"

if (-not (Test-Path $ArtifactsRoot)) {
    Write-Error "Orbit artifacts directory not found at $ArtifactsRoot"
    exit 1
}

# Set up PYTHONPATH
$env:PYTHONPATH = "$ArtifactsRoot;$env:PYTHONPATH"

# Locate Python
$pythonCmd = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonCmd) {
    $pythonCmd = (Get-Command py -ErrorAction SilentlyContinue).Source
}
if (-not $pythonCmd) {
    Write-Error "Python executable not found in PATH."
    exit 1
}

& $pythonCmd -m standalone.operator.cli @OperatorArgs
exit $LASTEXITCODE
