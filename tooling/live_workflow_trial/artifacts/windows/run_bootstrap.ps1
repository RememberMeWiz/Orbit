param(
  [Parameter(Mandatory=$true)][string]$Root,
  [Parameter(Mandatory=$true)][string]$Config,
  [Parameter(Mandatory=$true)][string]$ProjectId,
  [Parameter(Mandatory=$true)][string]$WorkflowId,
  [Parameter(Mandatory=$true)][string]$WorkItem
)
$ErrorActionPreference = "Stop"

# Resolve caller-relative paths before changing location. Resolve-Path is
# available in Windows PowerShell 5.1 and returns stable absolute filesystem
# paths for the already-existing package root and trusted configuration file.
$ResolvedRoot = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path
$ResolvedConfig = (Resolve-Path -LiteralPath $Config -ErrorAction Stop).Path

$ArtifactRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ArtifactRoot
try {
  & py -3 -m windows.bootstrap_cli --root $ResolvedRoot --config $ResolvedConfig --project-id $ProjectId --workflow-id $WorkflowId --work-item $WorkItem
  if ($LASTEXITCODE -ne 0) { throw "Orbit bootstrap failed with exit code $LASTEXITCODE" }
} finally {
  Pop-Location
}
