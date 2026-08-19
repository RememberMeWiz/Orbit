param(
  [string]$Root = "",
  [string]$Manifest = "",
  [string]$ProjectId = "",
  [string]$WorkflowId = "",
  [string]$WorkItem = "",
  [int]$Polls = 2,
  [double]$Interval = 0.30
)
$ErrorActionPreference = "Stop"
$ArtifactRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Root)) {
  $Root = Split-Path -Parent $ArtifactRoot
}
. (Join-Path $PSScriptRoot "python_launcher.ps1")

$CliArgs = @("-m", "windows.observation.cli", "--root", $Root, "--polls", "$Polls", "--interval", "$Interval")
if (-not [string]::IsNullOrWhiteSpace($Manifest)) { $CliArgs += @("--manifest", $Manifest) }
if (-not [string]::IsNullOrWhiteSpace($ProjectId)) { $CliArgs += @("--project-id", $ProjectId) }
if (-not [string]::IsNullOrWhiteSpace($WorkflowId)) { $CliArgs += @("--workflow-id", $WorkflowId) }
if (-not [string]::IsNullOrWhiteSpace($WorkItem)) { $CliArgs += @("--work-item", $WorkItem) }
Push-Location $ArtifactRoot
try {
  Invoke-OrbitPython -Arguments $CliArgs
  if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
} finally {
  Pop-Location
}
