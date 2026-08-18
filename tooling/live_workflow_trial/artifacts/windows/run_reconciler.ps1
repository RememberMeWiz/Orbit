param(
  [string]$Root = "",
  [int]$Polls = 2,
  [double]$Interval = 0.30
)
$ErrorActionPreference = "Stop"
$ArtifactRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Root)) {
  $Root = Split-Path -Parent $ArtifactRoot
}
Push-Location $ArtifactRoot
try {
  py -3 -m windows.observation.cli --root "$Root" --polls $Polls --interval $Interval
  if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
} finally {
  Pop-Location
}
